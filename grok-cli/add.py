#!/usr/bin/env python3
"""9r-bulk-add / grok-cli — push Grok accounts to 9router.

Modes:
  auto    inject SSO cookies + browser Continue/Allow (batch from pending)
  manual  export pending → account.txt (email + password) for hand login/connect
  split   sso-pending.txt vs sso-added.txt via 9router API
  status  counts only
"""
import sys, time, re, json, secrets, shutil, os, subprocess, glob, threading, queue as queue_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from playwright.sync_api import sync_playwright
import curl_cffi.requests as creq

# ── Config ────────────────────────────────────────────────────
_env = {}
_envfile = Path(__file__).parent / '.env'
if _envfile.exists():
    for line in _envfile.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            _env[k.strip()] = v.strip()

def _env_or(key, default=''):
    return _env.get(key, default)

ROOT = Path(__file__).parent
ROUTER9 = _env_or('ROUTER9_URL', 'https://your-9router.example').rstrip('/')
ROUTER9_PASS = _env_or('ROUTER9_PASS', '')
_DEVICE_CODE_GAP = max(3.0, float(_env_or('DEVICE_CODE_GAP', '4')))
# accounts: paste into sso-pending.txt (python setup creates empty files)
SSO_ADDED = ROOT / 'sso-added.txt'
SSO_PENDING = ROOT / 'sso-pending.txt'
ACCOUNT_TXT = ROOT / 'account.txt'
_sso_raw = _env_or('SSO_FILE', str(SSO_PENDING))
SSO_FILE = Path(_sso_raw)
if not SSO_FILE.is_absolute():
    SSO_FILE = (ROOT / SSO_FILE).resolve()
IS_MAC = sys.platform == 'darwin'
IS_LINUX = sys.platform.startswith('linux')
IS_WIN = sys.platform == 'win32'
HIDE_BROWSER = True

def _find_chrome():
    env = _env_or('CHROME_BIN', '')
    if env and Path(env).exists():
        return env
    candidates = [
        # Linux
        '/usr/bin/google-chrome-stable', '/usr/bin/google-chrome',
        '/usr/bin/chromium', '/usr/bin/chromium-browser',
        # macOS
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
        str(Path.home() / 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
    ]
    # Windows
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
def no(msg):
    with _print_lock:
        print(f"    {RED}✗{RST} {msg}")
def wait(msg):
    with _print_lock:
        print(f"    {YEL}→{RST} {msg}")

# ── 9Router API ───────────────────────────────────────────────
class Router9:
    def __init__(self):
        self.s = creq.Session()
        self.s.headers.update({'Accept': 'application/json', 'Content-Type': 'application/json'})

    def login(self):
        r = self.s.post(f'{ROUTER9}/api/auth/login', json={'password': ROUTER9_PASS}, timeout=15)
        return r.json().get('success', False)

    def device_code(self):
        return self.s.get(f'{ROUTER9}/api/oauth/grok-cli/device-code', timeout=15).json()

    def poll(self, device_code, code_verifier):
        return self.s.post(
            f'{ROUTER9}/api/oauth/grok-cli/poll',
            json={'deviceCode': device_code, 'codeVerifier': code_verifier},
            timeout=15,
        ).json()

    def list_grok(self):
        conns = self.s.get(f'{ROUTER9}/api/providers', timeout=20).json().get('connections', [])
        return [c for c in conns if c.get('provider') == 'grok-cli']

# ── SSO files ─────────────────────────────────────────────────
def load_accounts(path):
    path = Path(path)
    out = []
    if not path.exists():
        return out
    for l in path.read_text().splitlines():
        s = l.strip()
        if not s or s.startswith('#') or '"email"' not in s:
            continue
        try:
            out.append(json.loads(s))
        except Exception:
            pass
    return out

def promote_to_added(accs):
    if not accs:
        return
    if isinstance(accs, dict):
        accs = [accs]
    by = {a.get('email'): a for a in accs if a.get('email')}
    if not by:
        return
    with _out_lock:
        added = {a['email']: a for a in load_accounts(SSO_ADDED) if a.get('email')}
        added.update(by)
        SSO_ADDED.write_text(''.join(json.dumps(a) + '\n' for a in added.values()))
        if SSO_PENDING.exists():
            left = [a for a in load_accounts(SSO_PENDING) if a.get('email') not in by]
            SSO_PENDING.write_text(''.join(json.dumps(a) + '\n' for a in left))

def split_accounts(write=True):
    """Reconcile local pending+added files against 9router (no external sso dump)."""
    print(f"\n{CYN} ─── [ SPLIT ↔ 9ROUTER ] ──{'─'*44}{RST}")
    r9 = Router9()
    if not r9.login():
        no("9router login failed"); return
    ok("9router login")
    existing = {c.get('email') for c in r9.list_grok() if c.get('email')}

    # merge local files (pending + added + optional SSO_FILE)
    by = {}
    for path in (SSO_PENDING, SSO_ADDED, SSO_FILE):
        for a in load_accounts(path):
            e = a.get('email')
            if e:
                by[e] = a
    if not by:
        no("no local accounts — paste into sso-pending.txt (python setup)")
        print(f"  {GRN}di 9router (grok-cli){RST}: {len(existing)}")
        return

    unique = list(by.values())
    added = [a for a in unique if a['email'] in existing]
    pending = [a for a in unique if a['email'] not in existing]
    only_r = sorted(existing - set(by))
    print(f"  local unique : {len(unique)}")
    print(f"  {GRN}sudah di 9router{RST}: {len(added)}")
    print(f"  {YEL}belum (pending){RST}: {len(pending)}")
    if only_r:
        print(f"  {DIM}di 9router saja (tidak di file lokal): {len(only_r)}{RST}")
    if write:
        # preserve comment header in pending if any
        SSO_ADDED.write_text(''.join(json.dumps(a) + '\n' for a in added))
        body = ''.join(json.dumps(a) + '\n' for a in pending)
        SSO_PENDING.write_text(body)
        ok(f"wrote {SSO_ADDED.name} ({len(added)})")
        ok(f"wrote {SSO_PENDING.name} ({len(pending)})")
        wait("push: python run auto")

# ── Cookies / browser helpers ─────────────────────────────────
def _sso_cookies_pw(acc):
    out, sso_vals = [], {}
    for c in acc.get('sso_cookies') or []:
        cc = dict(c)
        if not cc.get('name') or not cc.get('value'):
            continue
        if not cc.get('domain'):
            cc['domain'] = '.x.ai'
        ss = cc.get('sameSite', 'Lax')
        cc['sameSite'] = ss if ss in ('Strict', 'Lax', 'None') else 'Lax'
        cc.setdefault('path', '/')
        for k in list(cc):
            if k not in ('name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite'):
                cc.pop(k, None)
        out.append(cc)
        n = cc['name']
        if n in ('sso', 'sso-rw') or n.startswith('sso'):
            if n not in sso_vals or 'x.ai' in (cc.get('domain') or ''):
                sso_vals[n] = cc
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

_device_lock = threading.Lock()
_device_last = 0.0

def _device_code_throttled(r9, retries=6):
    global _device_last
    last_err = None
    for i in range(retries):
        with _device_lock:
            now = time.time()
            gap = _DEVICE_CODE_GAP - (now - _device_last)
            if gap > 0:
                time.sleep(gap)
            try:
                d = r9.device_code()
            except Exception as e:
                last_err, d = e, {'error': str(e)}
            _device_last = time.time()
        blob = json.dumps(d) if isinstance(d, dict) else str(d)
        if d.get('verification_uri_complete') or d.get('device_code'):
            return d
        if any(x in blob.lower() for x in ('slow_down', 'too many', '429')):
            sleep_for = min(8 + i * 6, 45)
            wait(f"device-code slow_down — sleep {sleep_for}s ({i+1}/{retries})")
            time.sleep(sleep_for)
            last_err = blob[:80]
            continue
        return d
    return {'error': last_err or 'device-code failed'}

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

def _pw_kwargs(profile, hide=True):
    args = [
        '--no-sandbox', '--disable-dev-shm-usage',
        '--disable-blink-features=AutomationControlled',
        '--disable-gpu', '--disk-cache-size=1',
    ]
    if IS_LINUX:
        args += ['--ozone-platform=x11', '--disable-features=WaylandWindowDecorations']
    if hide:
        args += ['--window-position=-2400,-2400', '--window-size=800,600', '--start-minimized']
    kw = dict(
        user_data_dir=str(profile), headless=False, args=args,
        viewport={'width': 800, 'height': 600},
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
    for name in ('Allow All', 'Accept All', 'Accept All Cookies', 'Reject All'):
        try:
            page.get_by_role('button', name=name, exact=False).click(timeout=1000)
            time.sleep(0.3)
            return True
        except Exception:
            pass
    return False

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

def _approve_on_page(ctx, page, acc, d):
    try:
        ctx.clear_cookies()
    except Exception:
        pass
    page.goto('https://accounts.x.ai/', wait_until='domcontentloaded', timeout=25000)
    cookies = _sso_cookies_pw(acc)
    if not any(c.get('name') == 'sso' for c in cookies):
        return 'fail', 'no sso cookie'
    try:
        ctx.add_cookies(cookies)
    except Exception:
        for c in cookies:
            try:
                ctx.add_cookies([c])
            except Exception:
                pass
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

def _poll(r9, d, max_s=40):
    t0 = time.time()
    while time.time() - t0 < max_s:
        try:
            res = r9.poll(d['device_code'], d['codeVerifier'])
        except Exception as e:
            return 'fail', f'poll: {e}'
        if res.get('success'):
            return 'ok', 'added'
        if not res.get('pending'):
            return 'fail', f"poll: {res.get('error')}"
        time.sleep(1.2)
    return 'fail', 'poll timeout'

# ── AUTO mode (browser workers) ───────────────────────────────
def _worker(wid, job_q, existing, hide, stats, lock, total):
    r9 = Router9()
    if not r9.login():
        wait(f"w{wid}: login failed")
        return
    tmp_root = Path(os.environ.get('TEMP') or os.environ.get('TMP') or '/tmp')
    profile = tmp_root / f"9r-add-w{wid}-{os.getpid()}-{secrets.token_hex(3)}"
    env_saved = _force_x11_env() if (hide and IS_LINUX) else {}
    try:
        with sync_playwright() as p:
            wait(f"w{wid}: chrome ready")
            ctx = p.chromium.launch_persistent_context(**_pw_kwargs(profile, hide))
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
                        promote_to_added(acc)
                        ok(f"[{n}/{total}] w{wid} {email} skip")
                        job_q.task_done()
                        continue
                    try:
                        d = _device_code_throttled(r9)
                        if not d.get('verification_uri_complete'):
                            raise RuntimeError(f"device-code: {d.get('error') or d}")
                        st, msg = _approve_on_page(ctx, page, acc, d)
                        if st != 'ok':
                            raise RuntimeError(msg)
                        st, msg = _poll(r9, d, max_s=40)
                        if st != 'ok':
                            raise RuntimeError(msg)
                        with lock:
                            stats['added'] += 1
                            existing.add(email)
                            stats['done'] = stats.get('done', 0) + 1
                            n = stats['done']
                        promote_to_added(acc)
                        ok(f"[{n}/{total}] w{wid} {email} added")
                    except Exception as e:
                        err = str(e)
                        retryable = (
                            'sso expired' not in err.lower()
                            and 'no sso cookie' not in err.lower()
                            and any(x in err.lower() for x in (
                                'slow_down', 'too many', 'poll', 'timeout',
                                'no oauth', 'connection', 'target', 'browser',
                            ))
                        )
                        with lock:
                            stats['done'] = stats.get('done', 0) + 1
                            n = stats['done']
                            if retryable and stats.get('round', 1) < stats.get('rounds', 3):
                                stats.setdefault('retry', []).append(acc)
                                wait(f"[{n}/{total}] w{wid} {email} — {err[:55]} (retry)")
                            else:
                                stats['failed'] += 1
                                no(f"[{n}/{total}] w{wid} {email} — {err[:70]}")
                    finally:
                        job_q.task_done()
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        no(f"w{wid} crash: {e}")
    finally:
        if env_saved:
            _restore_env(env_saved)
        shutil.rmtree(profile, ignore_errors=True)

def run_auto(accounts, workers=2, hide=True, rounds=3):
    print(f"\n{CYN} ─── [ AUTO → 9ROUTER ] ──{'─'*42}{RST}")
    r9 = Router9()
    if not r9.login():
        no("9router login failed"); return
    ok("9router login")
    existing = {c.get('email') for c in r9.list_grok() if c.get('email')}

    seen, base = set(), []
    for a in accounts:
        e = a.get('email')
        if e and e not in seen:
            seen.add(e)
            base.append(a)

    workers = max(1, min(workers, 4))
    wait(f"queue {len(base)} | workers={workers} | rounds≤{rounds} | gap≥{_DEVICE_CODE_GAP}s")

    stats = {'added': 0, 'skipped': 0, 'failed': 0, 'done': 0, 'retry': [], 'rounds': rounds}
    lock = threading.Lock()
    todo = [a for a in base if a.get('email') not in existing]
    for a in base:
        if a.get('email') in existing:
            promote_to_added(a)
            with lock:
                stats['skipped'] += 1

    for rnd in range(1, rounds + 1):
        if not todo:
            break
        stats['round'] = rnd
        stats['retry'] = []
        if rnd > 1:
            wait(f"round {rnd}/{rounds}: retry {len(todo)}")
            time.sleep(3)
        total = len(todo)
        stats['done'] = 0
        q = queue_mod.Queue()
        for a in todo:
            q.put(a)
        with ThreadPoolExecutor(max_workers=min(workers, max(1, total))) as ex:
            futs = [
                ex.submit(_worker, i + 1, q, existing, hide, stats, lock, total)
                for i in range(min(workers, max(1, total)))
            ]
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    no(f"worker crash: {e}")
        todo = list(stats.get('retry') or [])

    for a in todo:
        no(f"give up: {a.get('email')}")
        stats['failed'] += 1

    print(f"  {GRN}added{RST}: {stats['added']}  {YEL}skipped{RST}: {stats['skipped']}  {RED}failed{RST}: {stats['failed']}")
    print(f"  files: {SSO_ADDED.name}={len(load_accounts(SSO_ADDED))}  {SSO_PENDING.name}={len(load_accounts(SSO_PENDING))}")

# ── MANUAL mode: export pending → account.txt ─────────────────
def run_manual(limit=None, fmt='colon'):
    """Export sso-pending.txt → account.txt (email + password only).

    User then logs into x.ai and connects 9router manually.
    Does not open browser or call device-code.
    """
    print(f"\n{CYN} ─── [ MANUAL EXPORT ] ──{'─'*44}{RST}")
    accounts = load_accounts(SSO_PENDING)
    if not accounts:
        no(f"no accounts in {SSO_PENDING.name} — paste JSON lines first (python setup)")
        return

    if limit is not None and limit > 0:
        accounts = accounts[:limit]

    lines = []
    skipped = 0
    for a in accounts:
        email = (a.get('email') or '').strip()
        password = (a.get('password') or '').strip()
        if not email:
            skipped += 1
            continue
        if not password:
            wait(f"skip {email} (no password field)")
            skipped += 1
            continue
        if fmt == 'space':
            lines.append(f"{email} {password}")
        else:
            lines.append(f"{email}:{password}")

    if not lines:
        no("nothing to export (need email + password on each pending line)")
        return

    ACCOUNT_TXT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    ok(f"wrote {ACCOUNT_TXT.name}  ({len(lines)} akun)")
    if skipped:
        wait(f"skipped {skipped} baris tanpa email/password")
    print(f"  format   : email:password  (satu baris per akun)")
    print(f"  path     : {ACCOUNT_TXT}")
    wait("login x.ai + connect 9router manual pakai daftar di account.txt")

# ── CLI ───────────────────────────────────────────────────────
def _flag_int(args, name, default):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args) and args[i + 1].lstrip('-').isdigit():
            return int(args[i + 1])
    return default

def main():
    global HIDE_BROWSER
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print("""
9r-bulk-add / grok-cli — push Grok accounts to 9router

  python setup                           # .env + sso-pending.txt + sso-added.txt
  python run split
  python run status
  python run auto [--workers N] [--rounds R] [--show]
  python run auto --from path.jsonl
  python run manual [N]                  # pending → account.txt (email:password)
  python run --os linux|mac|win …

  auto   = browser inject SSO + Continue/Allow
  manual = kamu login x.ai + connect 9router sendiri; skrip hanya export kredensial

Env (.env): ROUTER9_URL, ROUTER9_PASS, DEVICE_CODE_GAP, CHROME_BIN
""".strip())
        return

    cmd = args[0]
    HIDE_BROWSER = '--show' not in args

    if cmd in ('split', '--split'):
        split_accounts(write=True)
        return
    if cmd in ('status', '--status'):
        split_accounts(write=False)
        return

    if cmd in ('manual', '--manual', 'export', '--export'):
        n = None
        fmt = 'colon'
        for a in args[1:]:
            if a.lstrip('-').isdigit():
                n = int(a)
            elif a in ('--space',):
                fmt = 'space'
        run_manual(limit=n, fmt=fmt)
        return

    if cmd in ('auto', '--auto', 'add'):
        # default: sso-pending.txt (paste accounts there after setup)
        src = SSO_PENDING
        if '--from' in args:
            i = args.index('--from')
            if i + 1 < len(args):
                src = Path(args[i + 1])
                if not src.is_absolute():
                    src = (ROOT / src).resolve()
        elif '--all' in args:
            src = SSO_FILE
        accounts = load_accounts(src)
        skip = set()
        for name in ('--workers', '--rounds', '--from'):
            if name in args:
                i = args.index(name)
                skip.add(i)
                if i + 1 < len(args):
                    skip.add(i + 1)
        for i, a in enumerate(args):
            if i in skip or i == 0:
                continue
            if a.lstrip('-').isdigit():
                accounts = accounts[-int(a):]
                break
        workers = max(1, min(_flag_int(args, '--workers', 2), 4))
        rounds = max(1, _flag_int(args, '--rounds', 3))
        if not accounts:
            no(f"no accounts in {src} — paste JSON lines into sso-pending.txt (python setup)"); return
        wait(f"{len(accounts)} akun dari {src.name}")
        run_auto(accounts, workers=workers, hide=HIDE_BROWSER, rounds=rounds)
        return

    no(f"unknown command: {cmd} — try --help")

if __name__ == '__main__':
    main()
