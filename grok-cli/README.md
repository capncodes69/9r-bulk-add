# grok-cli

Bulk-add akun Grok CLI ke **9router** (device-code OAuth).

| Mode | Fungsi |
|------|--------|
| **auto** | SSO cookies + browser Continue/Allow (otomatis ke 9router) |
| **manual** | export `sso-pending.txt` → `account.txt` (`email:password`); **kamu** login x.ai + connect 9router |
| **split** / **status** | sudah di 9router vs belum |

Satu **`setup`** + satu **`run`** untuk semua OS (`--os` flag).

---

## Install

```bash
cd grok-cli
python3 -m venv .venv          # Windows: py -3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # Win: .venv\Scripts\pip …
.venv/bin/playwright install chrome
```

```bash
python setup                   # default --os auto
python setup --os win          # optional force
```

`setup` membuat (skip jika sudah ada):

| File | |
|------|--|
| `.env` | dari `.env.example` |
| `sso-pending.txt` | paste akun (JSON lines) |
| `sso-added.txt` | terisi otomatis saat sukses |

---

## Konfigurasi (`.env`)

```ini
ROUTER9_URL=https://9r.example.com
ROUTER9_PASS=your_password
DEVICE_CODE_GAP=4
# CHROME_BIN=...   # optional
```

Paste ke **`sso-pending.txt`** (satu JSON per baris):

```json
{"email":"user@example.com","sso_cookies":[{"name":"sso","value":"...","domain":".x.ai","path":"/","secure":true,"httpOnly":true,"sameSite":"Lax"}]}
```

---

## Perintah

```bash
python run split
python run status

# auto: browser inject SSO → 9router
python run auto
python run auto --workers 2 --rounds 3
python run auto --show

# manual: export pending → account.txt (email:password)
# kamu login x.ai + connect 9router sendiri
python run manual
python run manual 10
```

### Manual vs Auto

| | **auto** | **manual** |
|--|----------|------------|
| Browser | skrip (SSO inject) | **kamu** |
| Input | `sso-pending.txt` (butuh `sso_cookies`) | `sso-pending.txt` (butuh `email` + `password`) |
| Output | akun masuk 9router + pindah ke `sso-added.txt` | file **`account.txt`** |
| Format output | — | `email:password` per baris |

### Flag OS

```bash
python run --os auto auto          # deteksi (default)
python run --os linux auto
python run --os mac status
python run --os win auto --workers 2
```

| `--os` | Perilaku |
|--------|----------|
| `linux` | xvfb (jika ada) + force X11 |
| `mac` | Chrome `/Applications/...` |
| `win` | Chrome `Program Files\...\chrome.exe` |
| `auto` | deteksi `platform.system()` |

Windows juga: `py run auto` jika `python` tidak di PATH.

---

## Hide browser

Default: window off-screen / minimized (bukan headless).  
Debug: `python run auto --show`.

---

## Stop

```bash
# Linux/macOS
pkill -f 'add.py'
pkill -9 -f 'remote-debugging-port' 2>/dev/null || true
```

```powershell
# Windows — Task Manager, atau:
Get-Process python*,chrome* -ErrorAction SilentlyContinue | Stop-Process -Force
```

---

## Troubleshooting

| Gejala | Arti |
|--------|------|
| `SSO expired` | cookie/session invalid |
| `slow_down` | turunkan workers / naikkan `DEVICE_CODE_GAP` |
| `poll timeout` | Allow belum selesai — retry |
| `no sso cookie` | baris tanpa cookie `sso` |
| Chrome not found | set `CHROME_BIN` di `.env` |

---

## Notes

- Alur: `setup` → paste pending → `run auto` → sukses ke `sso-added.txt`  
- File `sso-*.txt` di-gitignore  
