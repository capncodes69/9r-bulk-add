# 9r-bulk-add

Bulk-add accounts to [9router](https://github.com) via OAuth device-code flow.

```
9r-bulk-add/
└── grok-cli/       ← Grok CLI (x.ai)
```

## Quick Start

```bash
cd grok-cli
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chrome

python main.py setup    # creates .env, sso-pending.txt, sso-added.txt
# edit .env → set ROUTER9_URL and ROUTER9_PASS
# paste accounts into sso-pending.txt

python main.py          # add pending accounts to 9router
python main.py split    # reconcile local files vs 9router
```

See [grok-cli/README.md](grok-cli/README.md) for details.
