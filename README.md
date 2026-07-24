# 9r-bulk-add

Bulk-add accounts to [9router](https://github.com) via the OAuth device-code
flow. One tool for all providers — selected via **flag** (not folders).

## Supported Providers

### `--grok` — Grok CLI (x.ai)

Adds Grok CLI accounts to 9router (provider slug: `grok-cli`).

- **Input**: `input/grok.txt` — one JSON per line with `sso_cookies[]`
  containing an `sso` cookie (grabbed from an x.ai session).
- **Flow**: warms up the session on `accounts.x.ai`, plants the sso cookies
  (fanned out to all x.ai / grok.com domains), opens the 9router device URL,
  clicks **Allow**, then polls 9router until linked.
- **Poll timeout**: 40s (fixed).

```json
{"email":"user@example.com","sso_cookies":[{"name":"sso","value":"...","domain":".x.ai","path":"/","secure":true,"httpOnly":true,"sameSite":"Lax"}]}
```

### `--qoder` — Qoder (qoder.com)

Adds Qoder accounts to 9router (provider slug: `qoder`).

- **Input**: `input/qoder.txt` — one JSON per line with `cookies[]`
  containing `qoder_session_cookie`.
- **Flow**: plants the account's qoder.com cookies, opens the 9router device
  URL directly (`qoder.com/device/selectAccounts?...`), clicks the account
  card matching the email, clicks **Authorize** (plus a second confirm when
  needed), then polls 9router until linked. If the device URL redirects to a
  login page, the session is warmed up once via `qoder.com` — if it still
  asks for login, the account is marked `SSO expired` (not retried).
- **Poll timeout**: `POLL_TIMEOUT` seconds (default 180).

```json
{"email":"user@example.com","cookies":[{"name":"qoder_session_cookie","value":"...","domain":".qoder.com","path":"/"}]}
```

### Summary

| Flag | Provider | 9router slug | Input | Output | Session cookie |
|------|----------|--------------|-----------------|----------------|----------------|
| `--grok` | Grok CLI (x.ai) | `grok-cli` | `input/grok.txt` | `output/grok.txt` | `sso` in `sso_cookies[]` |
| `--qoder` | Qoder (qoder.com) | `qoder` | `input/qoder.txt` | `output/qoder.txt` | `qoder_session_cookie` in `cookies[]` |

Once an account is linked, it moves from `input/` to `output/`.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium   # optional when using system Chrome
.venv/bin/python main.py setup
```

## Config (`.env`)

```ini
ROUTER9_URL=https://your-9router.example
ROUTER9_PASS=your_password
DEVICE_CODE_GAP=4
POLL_TIMEOUT=180
# CHROME_BIN=/usr/bin/google-chrome-stable
```

## Usage

```bash
.venv/bin/python main.py --grok                          # add grok accounts from input/
.venv/bin/python main.py --qoder                         # add qoder accounts from input/
.venv/bin/python main.py --qoder --workers 2 --rounds 3
.venv/bin/python main.py --qoder --show                  # visible browser (debug)
.venv/bin/python main.py --grok -v                       # verbose logs
.venv/bin/python main.py --qoder --from path.jsonl       # custom source file
.venv/bin/python main.py --qoder 5                       # only the last 5 accounts
.venv/bin/python main.py setup                           # first-time setup
.venv/bin/python main.py split  [--grok|--qoder]         # reconcile local vs 9router
.venv/bin/python main.py status [--grok|--qoder]         # read-only counts (both if no flag)
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `no sso cookie` / `no qoder_session_cookie` | Entry has no session — refresh that account |
| `SSO expired` | Cookie expired — refresh the account's cookies |
| `no OAuth Allow` / `no Authorize button` | Page slow/changed — retry, or debug with `--show` |
| `slow_down` | Lower `--workers` or raise `DEVICE_CODE_GAP` |
| `poll timeout` | Retry — the approve step may not have finished |
