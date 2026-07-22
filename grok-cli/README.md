# grok-cli

Bulk-add Grok CLI (x.ai) accounts to 9router via device-code OAuth.

Injects SSO cookies into Chrome, clicks Continue/Allow on x.ai, polls 9router until linked. Successfully added accounts move from `sso-pending.txt` to `sso-added.txt`.

## Install

```bash
cd grok-cli
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/python main.py setup
```

## Config (`.env`)

```ini
ROUTER9_URL=https://your-9router.example
ROUTER9_PASS=your_password
DEVICE_CODE_GAP=4
# CHROME_BIN=/usr/bin/google-chrome-stable
```

Paste accounts into `sso-pending.txt` (one JSON per line):

```json
{"email":"user@example.com","sso_cookies":[{"name":"sso","value":"...","domain":".x.ai","path":"/","secure":true,"httpOnly":true,"sameSite":"Lax"}]}
```

## Usage

```bash
.venv/bin/python main.py                          # add pending accounts to 9router
.venv/bin/python main.py --workers 2 --rounds 3
.venv/bin/python main.py --show                   # visible browser (debug)
.venv/bin/python main.py --from path.jsonl        # custom source file
.venv/bin/python main.py setup                    # first-time setup
.venv/bin/python main.py split                    # reconcile local vs 9router
.venv/bin/python main.py status                   # read-only counts
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `SSO expired` | Refresh cookies in `sso-pending.txt` |
| `slow_down` | Lower `--workers` or raise `DEVICE_CODE_GAP` |
| `poll timeout` | Retry — OAuth Allow may not have completed |
| `no sso cookie` | Entry missing `sso` cookie |
| Chrome not found | Set `CHROME_BIN` in `.env` |
