# 🔁 Looper Daemon

**Autonomous AI Software Engineering System** — menjalankan pipeline multi-agent
(Research → Architecture → Build → Test → Review → Security Audit → Fix →
Performance → Docs) secara otomatis dari satu goal, 24/7 di background.

Semua agent (kecuali Orchestrator) dipanggil lewat **OpenRouter**
(`https://openrouter.ai/api/v1`) — satu API key untuk banyak model LLM.

---

## ✨ Fitur

- **Pipeline lengkap**: 10 role agent (Researcher, Architect, UX/API Designer,
  Builder, Tester, Reviewer, Security Auditor, Performance Optimizer,
  Documentation Writer, Fixer).
- **Scoring engine** (0–100): build + test + security + review → keputusan
  otomatis retry / fix / finish.
- **Dua trigger**: file watcher (`looper_commands.txt`) dan HTTP API (`/build`).
- **Aman**: HTTP API bind `127.0.0.1` + bearer-token auth (default), tidak ada
  secret hardcode (API key dari environment variable).
- **Robust**: retry + exponential backoff pada tiap LLM call, state atomic-write,
  graceful handling bila agent gagal (tidak lolos secara palsu).
- **Teruji**: 23 unit/integration test (coverage ~82%) + CI GitHub Actions.

---

## 📋 Prasyarat

- Python **3.11+**
- Akun **OpenRouter** + API key (`https://openrouter.ai/keys`)

---

## 🚀 Instalasi

```bash
# 1. Clone
git clone https://github.com/masrizram/looper.git
cd looper

# 2. Install dependency
pip install -r requirements.txt

# 3. Siapkan config (sudah ada config.yaml default; edit sesuai kebutuhan)
#    Pastikan nama file config = config.yaml (atau looper_config.yaml)

# 4. Set environment variable (PENTING - jangan hardcode key di config!)
export OPENROUTER_API_KEY="sk-or-..."          # wajib
export LOOPER_HTTP_TOKEN="token_rahasia_anda"   # opsional, untuk auth API HTTP
```

> 💡 `config.yaml` hanya menyimpan **nama** environment variable
> (`api_key_env: OPENROUTER_API_KEY`), bukan key itu sendiri. Aman untuk di-commit.

---

## 🎮 Cara Penggunaan

### Mode 1 — Daemon (24/7, trigger via file atau HTTP)

```bash
python daemon.py --daemon
```

- **File watcher**: tulis goal ke `looper_commands.txt`, daemon otomatis memproses.
  ```bash
  echo "Build a FastAPI REST API for a todo app" > looper_commands.txt
  ```
- **HTTP API**: kirim POST ke `http://127.0.0.1:9999/build`
  ```bash
  curl -X POST http://127.0.0.1:9999/build \
    -H "Authorization: Bearer $LOOPER_HTTP_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"goal": "Build a FastAPI REST API for a todo app"}'
  ```

### Mode 2 — Sekali Jalan (single goal)

```bash
python daemon.py --goal "Build a FastAPI REST API for a todo app"
```

### Mode 3 — Reset State

```bash
python daemon.py --reset
```

### Opsi CLI Lain

| Flag | Fungsi |
|------|--------|
| `--goal "..."` | Jalankan satu build untuk goal tertentu |
| `--daemon` | Jalankan sebagai daemon (watcher + HTTP server) |
| `--reset` | Hapus state (`looper_state.json`) |
| `--config path.yaml` | Pakai file config khusus (default: `config.yaml`, fallback `looper_config.yaml`) |

---

## ⚙️ Konfigurasi (`config.yaml`)

```yaml
workspace: "./workspace"          # direktori hasil generate
state_file: "./looper_state.json"
watch_file: "./looper_commands.txt"

http:
  bind: "127.0.0.1"               # hanya localhost (aman). Ganti 0.0.0.0 HANYA di belakang proxy ber-auth
  port: 9999
  auth_token_env: "LOOPER_HTTP_TOKEN"   # nama env var untuk bearer token

execution:
  max_cycles: 5                   # batas siklus retry
  target_score: 99                # berhenti jika skor >= ini
  min_acceptable: 95              # di bawah ini -> jalankan fixer

openrouter:
  base_url: "https://openrouter.ai/api/v1"
  api_key_env: "OPENROUTER_API_KEY"   # HANYA nama env var
  site_url: ""
  site_name: "Looper Daemon"

agents:                           # ganti model kapan saja tanpa ubah kode
  builder:
    model: "anthropic/claude-3.5-sonnet"
  # ...lihat config.yaml lengkap
```

> 🔒 **Keamanan**: jangan pernah menulis API key langsung di `config.yaml`.
> Selalu lewat environment variable.

---

## 🧪 Testing & Lint

```bash
pytest                  # jalankan 23 test (tanpa butuh API key)
pytest --cov=daemon     # lihat coverage
black --check daemon.py tests/     # cek format
flake8 daemon.py tests/            # cek lint
```

CI GitHub Actions (`.github/workflows/ci.yml`) otomatis menjalankan lint + test
setiap push/PR.

---

## 🗂️ Struktur Output

Setelah build, hasil ada di `workspace/`:

```
workspace/
├── research.md
├── architecture/
│   └── design.md
├── src/
│   ├── generated_code.py
│   └── optimized_code.py
├── tests/
│   └── test_generated.py
├── review.md
├── security_audit.md
└── docs/
    └── README.md
```

State progres disimpan di `looper_state.json` (di-ignore oleh git).

---

## 📊 Skor Kualitas (hasil audit)

| Aspek | Skor |
|-------|:----:|
| Keamanan | 98 |
| Coding Standard | 100 |
| Performa | 95 |
| Konfigurasi | 100 |
| Maintainability | 98 |
| Testing | 100 |

Lihat `audit_result.md` untuk detail lengkap.

---

## 📝 Lisensi

MIT — bebas digunakan & dimodifikasi.
