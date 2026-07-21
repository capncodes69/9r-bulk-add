# 9r-bulk-add

Bulk-add accounts ke **9router**.

Tiap **provider** = satu folder. Jalankan script **di dalam folder provider** (tidak ada launcher di root).

```
9r-bulk-add/
├── README.md          ← ini
└── grok-cli/          ← provider Grok CLI (x.ai)
    ├── add.py
    ├── run.sh | run.bat | run.ps1
    ├── requirements.txt
    ├── .env.example
    └── README.md
```

Nanti provider lain: `codex/`, `claude/`, … pola yang sama.

---

## Provider

| Folder | Platform | Docs |
|--------|----------|------|
| [`grok-cli/`](grok-cli/) | Grok CLI / x.ai device-code | [grok-cli/README.md](grok-cli/README.md) |

---

## Cara pakai (umum)

1. Masuk folder provider  
2. Setup venv + `.env` (lihat README provider)  
3. Jalankan `./run.sh` / `run.bat` / `.\run.ps1`

```bash
# contoh Grok CLI — Linux/macOS
cd grok-cli
./run.sh split
./run.sh auto --pending --workers 2
./run.sh manual 3

# Windows
cd grok-cli
run.bat split
run.bat auto --pending
run.bat manual 3
```

---

## Catatan

- Signup tetap di `../grok-sign-up/`
- Kredensial / `sso-*.txt` di-gitignore per provider
- OS: Linux · macOS · Windows (detail di README masing-masing provider)
