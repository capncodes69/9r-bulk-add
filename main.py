#!/usr/bin/env python3
"""9r-bulk-add — push accounts to 9router via device-code OAuth.

One tool for all providers — selected via flag (not folders):
  --grok   Grok CLI (x.ai): inject sso cookies, click Allow
  --qoder  Qoder (qoder.com): inject cookies, pick account, click Authorize

Usage:
  python main.py --grok                            # add grok accounts from input/
  python main.py --qoder                           # add qoder accounts from input/
  python main.py --qoder --workers 2 --rounds 3
  python main.py --qoder --show                    # visible browser (debug)
  python main.py --grok -v                         # verbose logs
  python main.py --qoder --from path.jsonl         # custom source file
  python main.py setup                             # first-time setup (.env + data files)
  python main.py split  [--grok|--qoder]           # reconcile local vs 9router
  python main.py status [--grok|--qoder]           # read-only counts (both if no flag)
"""
import sys, time, json, secrets, shutil, os, threading, queue as queue_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from playwright.sync_api import sync_playwright
import curl_cffi.requests as creq

# ── Config ────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent

_env = {}
_envfile = ROOT / '.env'
if _envfile.exists():
    for line in _envfile.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            _env[k.strip()] = v.strip()

def _cfg(key, default=''):
    return _env.get(key, default)

ROUTER9 = _cfg('ROUTER9_URL', 'https://your-9router.example').rstrip('/')
ROUTER9_PASS = _cfg('ROUTER9_PASS', '')
_DEVICE_CODE_GAP = max(3.0, float(_cfg('DEVICE_CODE_GAP', '4')))
POLL_TIMEOUT = int(_cfg('POLL_TIMEOUT', '180'))   # qoder (grok uses fixed 40s)
INPUT_DIR = ROOT / 'input'     # accounts to add (paste JSON lines here)
OUTPUT_DIR = ROOT / 'output'   # accounts successfully linked
IS_MAC = sys.platform == 'darwin'
IS_LINUX = sys.platform.startswith('linux')
IS_WIN = sys.platform == 'win32'
VERBOSE = False

def _find_chrome():
    env = _cfg('CHROME_BIN', '')
    if env and Path(env).exists():
        return env
    candidates = [
        '/usr/bin/google-chrome-stable', '/usr/bin/google-chrome',
        '/usr/bin/chromium', '/usr/bin/chromium-browser',
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
        str(Path.home() / 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
    ]
    if IS_WIN:
        pf = os.environ.get('PROGRAMFILES', r'C:\Program Files')
        pf86 = os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')
        local = os.environ.get('LOCALAPPDATA', '')
        candidates = [
            str(Path(pf) / 'Google/Chrome/Application/chrome.exe'),
            str(Path(pf86) / 'Google/Chrome/Application/chrome.exe'),
            str(Path(local) / 'Google/Chrome/Application/chrome.exe') if local else '',
            str(Path(pf) / 'Chromium/Application/chrome.exe'),
        ] + candidates
    for p in candidates:
        if p and Path(p).exists():
            return p
    return None

CHROME_BIN = _find_chrome()

# ── ANSI ──────────────────────────────────────────────────────
GRN, RED, YEL, CYN, DIM, RST = \
    '\033[32m', '\033[31m', '\033[33m', '\033[36m', '\033[2m', '\033[0m'
_print_lock = threading.Lock()
_out_lock = threading.Lock()

def ok(msg):
    with _print_lock:
        print(f"    {GRN}✓{RST} {msg}")
def fail(msg):
    with _print_lock:
        print(f"    {RED}✗{RST} {msg}")
def info(msg):
    with _print_lock:
        print(f"    {YEL}→{RST} {msg}")
def vlog(msg):
    if VERBOSE:
        with _print_lock:
            print(f"      {DIM}· {msg}{RST}")

# ── 9Router API (provider slug dipassing) ─────────────────────
class Router9:
    def __init__(self):
        self.s = creq.Session()
        self.s.headers.update({'Accept': 'application/json', 'Content-Type': 'application/json'})

    def login(self):
        r = self.s.post(f'{ROUTER9}/api/auth/login', json={'password': ROUTER9_PASS}, timeout=15)
        return r.json().get('success', False)

    def device_code(self, slug):
        return self.s.get(f'{ROUTER9}/api/oauth/{slug}/device-code', timeout=15).json()

    def poll(self, slug, device_code, code_verifier):
        return self.s.post(
            f'{ROUTER9}/api/oauth/{slug}/poll',
            json={'deviceCode': device_code, 'codeVerifier': code_verifier},
            timeout=15,
        ).json()

    def list_conns(self, slug):
        conns = self.s.get(f'{ROUTER9}/api/providers', timeout=20).json().get('connections', [])
        return [c for c in conns if c.get('provider') == slug]

# ── Account files ─────────────────────────────────────────────
def _write_lines(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

def load_accounts(path):
    path = Path(path)
    out = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith('#') or '"email"' not in s:
            continue
        try:
            out.append(json.loads(s))
        except Exception:
            pass
    return out

def promote_to_output(prof, accs):
    if not accs:
        return
    if isinstance(accs, dict):
        accs = [accs]
    by = {a.get('email'): a for a in accs if a.get('email')}
    if not by:
        return
    with _out_lock:
        out_map = {a['email']: a for a in load_accounts(prof['output']) if a.get('email')}
        out_map.update(by)
        _write_lines(prof['output'], ''.join(json.dumps(a) + '\n' for a in out_map.values()))
        if prof['input'].exists():
            left = [a for a in load_accounts(prof['input']) if a.get('email') not in by]
            _write_lines(prof['input'], ''.join(json.dumps(a) + '\n' for a in left))

# ── Split / Status ────────────────────────────────────────────
def cmd_split(prof, write=True):
    """Reconcile local input/output files against 9router."""
    print(f"\n{CYN} ─── [ SPLIT ↔ 9ROUTER ({prof['label']}) ] ──{'─'*30}{RST}")
    r9 = Router9()
    if not r9.login():
        fail("9router login failed"); return
    ok("9router login")
    existing = {c.get('email') for c in r9.list_conns(prof['slug']) if c.get('email')}

    by = {}
    for path in (prof['input'], prof['output']):
        for a in load_accounts(path):
            e = a.get('email')
            if e:
                by[e] = a
    if not by:
        fail(f"no local accounts — paste into {prof['input'].name} (python main.py setup)")
        print(f"  {GRN}in 9router ({prof['slug']}){RST}: {len(existing)}")
        return

    unique = list(by.values())
    linked = [a for a in unique if a['email'] in existing]
    queued = [a for a in unique if a['email'] not in existing]
    only_r = sorted(existing - set(by))
    print(f"  local unique : {len(unique)}")
    print(f"  {GRN}already in 9router{RST}: {len(linked)}")
    print(f"  {YEL}not yet (input){RST}: {len(queued)}")
    if only_r:
        print(f"  {DIM}in 9router only (not in local files): {len(only_r)}{RST}")
    if write:
        _write_lines(prof['output'], ''.join(json.dumps(a) + '\n' for a in linked))
        _write_lines(prof['input'], ''.join(json.dumps(a) + '\n' for a in queued))
        ok(f"wrote {prof['output'].name} ({len(linked)})")
        ok(f"wrote {prof['input'].name} ({len(queued)})")

# ── Setup ─────────────────────────────────────────────────────
def cmd_setup():
    """First-time setup: .env + data files for all providers."""
    env_file = ROOT / '.env'
    env_example = ROOT / '.env.example'
    print(f"\n{CYN} ─── [ SETUP ] ──{'─'*52}{RST}")
    if env_file.exists():
        print("    .env already exists — skip")
    elif env_example.exists():
        shutil.copy(env_example, env_file)
        ok("created .env from .env.example  (edit ROUTER9_URL / ROUTER9_PASS)")
    else:
        fail(".env.example missing")

    for prof in PROFILES.values():
        for path, header in ((prof['input'], prof['input_header']), (prof['output'], '')):
            if path.exists():
                print(f"    {path.name} already exists — skip")
            else:
                _write_lines(path, header)
                note = 'paste accounts here' if header else 'auto-filled on success'
                ok(f"created {path.name}  ({note})")

    print(f"\n    1) edit .env (ROUTER9_URL / ROUTER9_PASS)")
    print(f"    2) paste accounts into input/grok.txt / input/qoder.txt")
    print(f"    3) python main.py --grok   /   python main.py --qoder")

# ── Browser helpers (shared) ──────────────────────────────────
def _force_x11_env():
    saved = {k: os.environ.get(k) for k in (
        'WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'ELECTRON_OZONE_PLATFORM_HINT',
        'GDK_BACKEND', 'QT_QPA_PLATFORM', 'OZONE_PLATFORM',
    )}
    for k in ('WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'ELECTRON_OZONE_PLATFORM_HINT'):
        os.environ.pop(k, None)
    os.environ['GDK_BACKEND'] = 'x11'
    os.environ['QT_QPA_PLATFORM'] = 'xcb'
    os.environ['OZONE_PLATFORM'] = 'x11'
    return saved

def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

def _pw_kwargs(profile_dir, prof, hide=True):
    w, h = prof['viewport']
    args = [
        '--no-sandbox', '--disable-dev-shm-usage',
        '--disable-blink-features=AutomationControlled',
        '--disable-gpu', '--disk-cache-size=1',
    ]
    if IS_LINUX:
        args += ['--ozone-platform=x11', '--disable-features=WaylandWindowDecorations']
    if hide:
        args += [f'--window-position=-2400,-2400', f'--window-size={w},{h}', '--start-minimized']
    kw = dict(
        user_data_dir=str(profile_dir), headless=False, args=args,
        viewport={'width': w, 'height': h},
        ignore_default_args=['--enable-automation'],
    )
    bin_l = (CHROME_BIN or '').lower()
    if CHROME_BIN and ('chrome.app' in bin_l or bin_l.endswith('chrome.exe') or 'chromium' in bin_l):
        if 'google-chrome' in bin_l and not IS_WIN:
            kw['channel'] = 'chrome'
        else:
            kw['executable_path'] = CHROME_BIN
    elif CHROME_BIN and 'google-chrome' in bin_l:
        kw['channel'] = 'chrome'
    elif CHROME_BIN:
        kw['executable_path'] = CHROME_BIN
    else:
        kw['channel'] = 'chrome'
    return kw

def _dismiss_cookies(page):
    for name in ('Allow All', 'Accept All', 'Accept All Cookies', 'Accept all',
                 'Accept', 'Agree', 'Reject All'):
        try:
            page.get_by_role('button', name=name, exact=False).click(timeout=1000)
            time.sleep(0.3)
            return True
        except Exception:
            pass
    return False

def _norm_cookie(cc, default_domain):
    """Normalize one cookie dict to playwright format. None if invalid."""
    cc = dict(cc)
    if not cc.get('name') or not cc.get('value'):
        return None
    if not cc.get('domain'):
        cc['domain'] = default_domain
    ss = str(cc.get('sameSite') or 'Lax')
    mp = {'strict': 'Strict', 'lax': 'Lax', 'none': 'None',
          'no_restriction': 'None', 'unspecified': 'Lax'}
    cc['sameSite'] = mp.get(ss.lower(), ss if ss in ('Strict', 'Lax', 'None') else 'Lax')
    cc.setdefault('path', '/')
    if 'expires' in cc and (not cc['expires'] or cc['expires'] <= 0):
        cc.pop('expires', None)
    for k in list(cc):
        if k not in ('name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite'):
            cc.pop(k, None)
    return cc

def _plant_cookies(ctx, cookies):
    try:
        ctx.add_cookies(cookies)
    except Exception:
        for c in cookies:
            try:
                ctx.add_cookies([c])
            except Exception:
                pass

# ── Provider: GROK (x.ai / grok-cli) ──────────────────────────
def _grok_has_session(a):
    return any(c.get('name') == 'sso' and c.get('value')
               for c in a.get('sso_cookies') or [])

def _grok_cookies_pw(acc):
    out, sso_vals = [], {}
    for c in acc.get('sso_cookies') or []:
        cc = _norm_cookie(c, '.x.ai')
        if not cc:
            continue
        out.append(cc)
        n = cc['name']
        if n in ('sso', 'sso-rw') or n.startswith('sso'):
            if n not in sso_vals or 'x.ai' in (cc.get('domain') or ''):
                sso_vals[n] = cc
    # fan out sso cookies to all x.ai / grok.com domains
    for n, base in sso_vals.items():
        for dom in ('.x.ai', 'accounts.x.ai', '.accounts.x.ai', '.grok.com'):
            if any(x.get('name') == base['name'] and x.get('domain') == dom for x in out):
                continue
            out.append({
                'name': base['name'], 'value': base['value'], 'domain': dom,
                'path': base.get('path') or '/', 'secure': True,
                'httpOnly': bool(base.get('httpOnly', True)),
                'sameSite': 'Lax',
            })
    return out

def _is_login(page):
    try:
        return bool(page.evaluate("""() => {
          const t = (document.body && document.body.innerText) || '';
          if (/Login with email|Login with Google/i.test(t) && /Login with/i.test(t)) return true;
          return !!document.querySelector('input[type=email],input[type=password],input[name=email]');
        }"""))
    except Exception:
        return False

def _click_allow(page):
    _dismiss_cookies(page)
    if _is_login(page):
        return 'login'
    try:
        page.get_by_role('button', name='Continue', exact=False).click(timeout=2500)
        time.sleep(0.5)
    except Exception:
        pass
    _dismiss_cookies(page)
    if _is_login(page):
        return 'login'
    for _ in range(3):
        try:
            page.get_by_role('button', name='Allow', exact=True).click(timeout=3500)
            return 'ok'
        except Exception:
            pass
        try:
            if page.evaluate("""() => {
              const b = [...document.querySelectorAll('button,[role=button]')].find(n => {
                const t = (n.innerText||'').replace(/\\s+/g,' ').trim();
                return t === 'Allow' || t === 'Authorize';
              });
              if (b) { b.click(); return true; }
              return false;
            }"""):
                return 'ok'
        except Exception:
            pass
        time.sleep(0.6)
        if _is_login(page):
            return 'login'
    return 'fail'

def _approve_grok(ctx, page, acc, d, prof):
    try:
        ctx.clear_cookies()
    except Exception:
        pass
    page.goto('https://accounts.x.ai/', wait_until='domcontentloaded', timeout=25000)
    cookies = prof['cookies_pw'](acc)
    if not any(c.get('name') == 'sso' for c in cookies):
        return 'fail', 'no sso cookie'
    _plant_cookies(ctx, cookies)
    page.goto(d['verification_uri_complete'], wait_until='domcontentloaded', timeout=35000)
    time.sleep(0.8)
    _dismiss_cookies(page)
    if _is_login(page):
        return 'fail', 'SSO expired'
    res = _click_allow(page)
    if res == 'login':
        return 'fail', 'SSO expired'
    if res != 'ok':
        return 'fail', 'no OAuth Allow'
    time.sleep(0.3)
    return 'ok', acc.get('email', '')

# ── Provider: QODER (qoder.com) ───────────────────────────────
def _qoder_has_session(a):
    return any(c.get('name') == 'qoder_session_cookie' and c.get('value')
               for c in a.get('cookies') or [])

def _qoder_cookies_pw(acc):
    out = []
    for c in acc.get('cookies') or []:
        cc = _norm_cookie(c, '.qoder.com')
        if cc:
            out.append(cc)
    return out

def _needs_login(url):
    u = (url or '').lower()
    return 'sign-in' in u or 'sign_in' in u or ('login' in u and 'device' not in u)

def _select_account(page, email):
    """On the selectAccounts page, click the account card matching email (or the first)."""
    try:
        return page.evaluate(r"""(email) => {
          email = String(email || '').toLowerCase();
          function clickable(n){
            while (n && n !== document.body){
              const s = getComputedStyle(n);
              if (n.tagName === 'BUTTON' || n.getAttribute('role') === 'button' ||
                  n.tagName === 'A' || s.cursor === 'pointer') return n;
              n = n.parentElement;
            }
            return null;
          }
          let nodes = Array.from(document.querySelectorAll('*')).filter(n => {
            const t = (n.innerText || n.textContent || '').toLowerCase();
            return email && t.includes(email) && n.children.length <= 3;
          });
          if (nodes.length){
            const c = clickable(nodes[0]) || nodes[0];
            c.click(); return 'email:' + ((c.innerText || '').slice(0, 40));
          }
          const cards = Array.from(document.querySelectorAll(
            '.ant-list-item,[class*="account"],[class*="Account"],li,[role="button"],button'
          )).filter(n => {
            const s = getComputedStyle(n);
            const r = n.getBoundingClientRect();
            return s.display !== 'none' && r.width > 40 && r.height > 20;
          });
          if (cards.length){ cards[0].click(); return 'card:' + ((cards[0].innerText || '').slice(0, 40)); }
          return '';
        }""", email or '') or ''
    except Exception:
        return ''

_AUTH_KEYWORDS = ['Authorize', 'Approve', 'Allow', 'Confirm', 'Continue',
                  'Sign in', 'Log in', 'Login', 'Agree', 'Yes', 'Grant',
                  'Trust', '授权', '确认', '继续', '允许', '登录']

def _click_authorize(page, tries=1):
    """Click the visible Authorize/Confirm button. Return True if clicked."""
    for _ in range(tries):
        try:
            hit = page.evaluate(r"""(kws) => {
              function isVisible(n){
                if (!n) return false;
                const s = getComputedStyle(n);
                if (s.display === 'none' || s.visibility === 'hidden') return false;
                const r = n.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              }
              const keys = kws.map(k => k.replace(/\s+/g, '').toLowerCase());
              const btns = Array.from(document.querySelectorAll(
                'button,[role="button"],input[type="submit"]'
              )).filter(n => isVisible(n) && !n.disabled);
              const b = btns.find(n => {
                const t = (n.innerText || n.textContent || n.value || '').replace(/\s+/g, '').toLowerCase();
                return keys.some(k => t.includes(k));
              });
              if (!b) return false;
              b.click(); return true;
            }""", _AUTH_KEYWORDS)
            if hit:
                return True
        except Exception:
            pass
        time.sleep(0.6)
    return False

def _approve_qoder(ctx, page, acc, d, prof):
    url = d.get('verification_uri_complete') or d.get('verification_uri')
    try:
        ctx.clear_cookies()
    except Exception:
        pass
    cookies = prof['cookies_pw'](acc)
    if not any(c['name'] == 'qoder_session_cookie' for c in cookies):
        return 'fail', 'no qoder_session_cookie'
    _plant_cookies(ctx, cookies)

    # open the device authorize URL DIRECTLY (cookies already planted in context)
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=35000)
    except Exception as e:
        vlog(f"goto device url: {str(e)[:60]}")
    time.sleep(1.2)
    _dismiss_cookies(page)

    # if redirected to a login page: warm up the session via qoder.com ONCE,
    # re-plant cookies, retry the device URL. Still login → session expired.
    if _needs_login(page.url):
        vlog("device URL asks for login — one-time fallback via qoder.com")
        try:
            page.goto('https://qoder.com/', wait_until='domcontentloaded', timeout=25000)
        except Exception:
            pass
        time.sleep(1.0)
        _plant_cookies(ctx, cookies)
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=35000)
        except Exception:
            pass
        time.sleep(1.2)
        _dismiss_cookies(page)
        if _needs_login(page.url):
            return 'fail', 'SSO expired'

    # pick the account (if a selectAccounts list is shown)
    picked = _select_account(page, acc.get('email', ''))
    if picked:
        vlog(f"picked account: {picked}")
        time.sleep(0.7)

    # click Authorize / Confirm
    if not _click_authorize(page, tries=2):
        return 'fail', 'no Authorize button'
    time.sleep(1.0)

    # sometimes a second confirm click is needed
    if _click_authorize(page):
        vlog("second confirm click")
        time.sleep(0.8)
    return 'ok', acc.get('email', '')

# ── Provider profiles ─────────────────────────────────────────
PROFILES = {
    'grok': {
        'label': 'GROK',
        'slug': 'grok-cli',
        'input': INPUT_DIR / 'grok.txt',
        'output': OUTPUT_DIR / 'grok.txt',
        'viewport': (800, 600),
        'poll_timeout': 40,
        'session_name': 'sso',
        'session_err': 'no sso cookie',
        'has_session': _grok_has_session,
        'cookies_pw': _grok_cookies_pw,
        'approve': _approve_grok,
        'input_header': """\
# Paste account JSON lines below (one per line).
# Example:
# {"email":"user@example.com","sso_cookies":[{"name":"sso","value":"...","domain":".x.ai","path":"/","secure":true,"httpOnly":true,"sameSite":"Lax"}]}
""",
    },
    'qoder': {
        'label': 'QODER',
        'slug': 'qoder',
        'input': INPUT_DIR / 'qoder.txt',
        'output': OUTPUT_DIR / 'qoder.txt',
        'viewport': (1280, 900),
        'poll_timeout': POLL_TIMEOUT,
        'session_name': 'qoder_session_cookie',
        'session_err': 'no qoder_session_cookie',
        'has_session': _qoder_has_session,
        'cookies_pw': _qoder_cookies_pw,
        'approve': _approve_qoder,
        'input_header': """\
# Paste account JSON lines below (one per line). Each entry must have
# cookies[] containing qoder_session_cookie.
# Example:
# {"email":"user@example.com","cookies":[{"name":"qoder_session_cookie","value":"...","domain":".qoder.com","path":"/"}]}
""",
    },
}

# ── Device-code throttle (shared across workers) ──────────────
_device_lock = threading.Lock()
_device_last = 0.0

def _device_code_throttled(r9, slug, retries=6):
    global _device_last
    last_err = None
    for i in range(retries):
        with _device_lock:
            now = time.time()
            gap = _DEVICE_CODE_GAP - (now - _device_last)
            if gap > 0:
                time.sleep(gap)
            try:
                d = r9.device_code(slug)
            except Exception as e:
                last_err, d = e, {'error': str(e)}
            _device_last = time.time()
        blob = json.dumps(d) if isinstance(d, dict) else str(d)
        if d.get('verification_uri_complete') or d.get('device_code'):
            return d
        if any(x in blob.lower() for x in ('slow_down', 'too many', '429')):
            sleep_for = min(8 + i * 6, 45)
            info(f"device-code slow_down — sleep {sleep_for}s ({i+1}/{retries})")
            time.sleep(sleep_for)
            last_err = blob[:80]
            continue
        return d
    return {'error': last_err or 'device-code failed'}

# ── Poll ──────────────────────────────────────────────────────
_POLL_STOP = ('access_denied', 'expired_token', 'invalid_grant', 'invalid_request')

def _poll(r9, d, prof):
    max_s = prof['poll_timeout']
    slug = prof['slug']
    interval = max(2, int(d.get('interval', 2) or 2))
    t0 = time.time()
    while time.time() - t0 < max_s:
        try:
            res = r9.poll(slug, d['device_code'], d.get('codeVerifier') or d.get('code_verifier'))
        except Exception as e:
            return 'fail', f'poll: {e}'
        if res.get('success'):
            return 'ok', 'linked'
        err = (res.get('error') or '').lower()
        if err == 'slow_down':
            interval += 2
        elif err == 'authorization_pending' or res.get('pending'):
            pass                                    # still waiting for authorize
        elif err:
            return 'fail', f'poll: {err}'
        else:
            return 'fail', f"poll: {res.get('error') or 'rejected'}"
        time.sleep(interval)
    return 'fail', 'poll timeout'

# ── Workers ───────────────────────────────────────────────────
def _worker(wid, job_q, existing, hide, stats, lock, total, prof):
    r9 = Router9()
    if not r9.login():
        info(f"w{wid}: login failed")
        return
    tmp_root = Path(os.environ.get('TEMP') or os.environ.get('TMP') or '/tmp')
    profile_dir = tmp_root / f"9r-{prof['slug']}-w{wid}-{os.getpid()}-{secrets.token_hex(3)}"
    env_saved = _force_x11_env() if (hide and IS_LINUX) else {}
    try:
        with sync_playwright() as p:
            info(f"w{wid}: chrome ready")
            ctx = p.chromium.launch_persistent_context(**_pw_kwargs(profile_dir, prof, hide))
            page = ctx.new_page()
            try:
                while True:
                    try:
                        acc = job_q.get_nowait()
                    except Exception:
                        break
                    email = acc.get('email', '')
                    if email in existing:
                        with lock:
                            stats['skipped'] += 1
                            stats['done'] = stats.get('done', 0) + 1
                            n = stats['done']
                        promote_to_output(prof, acc)
                        ok(f"[{n}/{total}] w{wid} {email} skip")
                        job_q.task_done()
                        continue
                    try:
                        if not prof['has_session'](acc):
                            raise RuntimeError(prof['session_err'])
                        d = _device_code_throttled(r9, prof['slug'])
                        if not (d.get('verification_uri_complete') or d.get('verification_uri')):
                            raise RuntimeError(f"device-code: {d.get('error') or d}")
                        d['codeVerifier'] = d.get('codeVerifier') or d.get('code_verifier')
                        st, msg = prof['approve'](ctx, page, acc, d, prof)
                        if st != 'ok':
                            raise RuntimeError(msg)
                        st, msg = _poll(r9, d, prof)
                        if st != 'ok':
                            raise RuntimeError(msg)
                        with lock:
                            stats['linked'] += 1
                            existing.add(email)
                            stats['done'] = stats.get('done', 0) + 1
                            n = stats['done']
                        promote_to_output(prof, acc)
                        ok(f"[{n}/{total}] w{wid} {email} linked")
                    except Exception as e:
                        err = str(e)
                        low = err.lower()
                        retryable = (
                            'sso expired' not in low
                            and 'no sso cookie' not in low
                            and 'no qoder_session_cookie' not in low
                            and any(x in low for x in (
                                'slow_down', 'too many', 'poll', 'timeout', 'no oauth',
                                'device-code', 'authorize', 'connection',
                                'browser', 'target',
                            ))
                        )
                        with lock:
                            stats['done'] = stats.get('done', 0) + 1
                            n = stats['done']
                            if retryable and stats.get('round', 1) < stats.get('rounds', 3):
                                stats.setdefault('retry', []).append(acc)
                                info(f"[{n}/{total}] w{wid} {email} — {err[:55]} (retry)")
                            else:
                                stats['failed'] += 1
                                fail(f"[{n}/{total}] w{wid} {email} — {err[:70]}")
                    finally:
                        job_q.task_done()
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        fail(f"w{wid} crash: {e}")
    finally:
        if env_saved:
            _restore_env(env_saved)
        shutil.rmtree(profile_dir, ignore_errors=True)

def cmd_add(accounts, prof, workers=2, hide=True, rounds=3):
    print(f"\n{CYN} ─── [ ADD → 9ROUTER ({prof['label']}) ] ──{'─'*36}{RST}")
    r9 = Router9()
    if not r9.login():
        fail("9router login failed"); return
    ok("9router login")
    existing = {c.get('email') for c in r9.list_conns(prof['slug']) if c.get('email')}

    seen, base = set(), []
    for a in accounts:
        e = a.get('email')
        if e and e not in seen:
            seen.add(e)
            base.append(a)

    workers = max(1, min(workers, 4))
    info(f"queue {len(base)} | workers={workers} | rounds≤{rounds} | gap≥{_DEVICE_CODE_GAP}s")

    stats = {'linked': 0, 'skipped': 0, 'failed': 0, 'done': 0, 'retry': [], 'rounds': rounds}
    lock = threading.Lock()
    todo = [a for a in base if a.get('email') not in existing]
    for a in base:
        if a.get('email') in existing:
            promote_to_output(prof, a)
            with lock:
                stats['skipped'] += 1

    for rnd in range(1, rounds + 1):
        if not todo:
            break
        stats['round'] = rnd
        stats['retry'] = []
        if rnd > 1:
            info(f"round {rnd}/{rounds}: retry {len(todo)}")
            time.sleep(3)
        total = len(todo)
        stats['done'] = 0
        q = queue_mod.Queue()
        for a in todo:
            q.put(a)
        with ThreadPoolExecutor(max_workers=min(workers, max(1, total))) as ex:
            futs = [
                ex.submit(_worker, i + 1, q, existing, hide, stats, lock, total, prof)
                for i in range(min(workers, max(1, total)))
            ]
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    fail(f"worker crash: {e}")
        todo = list(stats.get('retry') or [])

    for a in todo:
        fail(f"give up: {a.get('email')}")
        stats['failed'] += 1

    print(f"  {GRN}linked{RST}: {stats['linked']}  {YEL}skipped{RST}: {stats['skipped']}  {RED}failed{RST}: {stats['failed']}")
    print(f"  files: {prof['output'].name}={len(load_accounts(prof['output']))}  {prof['input'].name}={len(load_accounts(prof['input']))}")

# ── CLI ───────────────────────────────────────────────────────
def _flag_int(args, name, default):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args) and args[i + 1].lstrip('-').isdigit():
            return int(args[i + 1])
    return default

def main():
    global VERBOSE
    args = sys.argv[1:]
    if args and args[0] in ('-h', '--help'):
        print(__doc__.strip())
        return

    # provider flags (allowed in any position)
    provs = [p for p in PROFILES if f'--{p}' in args]
    args = [a for a in args if a not in (f'--{p}' for p in PROFILES)]
    cmd = args[0] if args else None

    if cmd == 'setup':
        cmd_setup()
        return
    if cmd in ('split', 'status'):
        targets = provs or list(PROFILES)
        for name in targets:
            cmd_split(PROFILES[name], write=(cmd == 'split'))
        return

    # default: add accounts — exactly 1 provider required
    if len(provs) != 1:
        fail("choose a provider: --grok or --qoder  (e.g. python main.py --qoder)")
        return
    prof = PROFILES[provs[0]]

    VERBOSE = '-v' in args or '--verbose' in args
    hide = '--show' not in args
    src = prof['input']
    if '--from' in args:
        i = args.index('--from')
        if i + 1 < len(args):
            src = Path(args[i + 1])
            if not src.is_absolute():
                src = (ROOT / src).resolve()
    accounts = load_accounts(src)
    skip = set()
    for name in ('--workers', '--rounds', '--from'):
        if name in args:
            i = args.index(name)
            skip.add(i)
            if i + 1 < len(args):
                skip.add(i + 1)
    for i, a in enumerate(args):
        if i in skip:
            continue
        if a.lstrip('-').isdigit():
            accounts = accounts[-int(a):]
            break
    workers = max(1, min(_flag_int(args, '--workers', 2), 4))
    rounds = max(1, _flag_int(args, '--rounds', 3))
    if not accounts:
        fail(f"no accounts in {src} — paste JSON lines into {prof['input'].name} (python main.py setup)")
        return
    n_sess = sum(1 for a in accounts if prof['has_session'](a))
    info(f"{len(accounts)} accounts from {src.name} ({n_sess} with {prof['session_name']})")
    cmd_add(accounts, prof, workers=workers, hide=hide, rounds=rounds)

if __name__ == '__main__':
    main()
