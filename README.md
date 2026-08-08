# Looper Daemon

An autonomous multi-agent pipeline that turns a one-sentence goal into working,
reviewed, security-audited software. Ten specialised LLM agents run in a scored
loop: build → test → review → audit → fix, repeating until the artifact clears
a release gate or the cycle budget runs out.

> **Read this first:** `POST /build` runs code written by a language model.
> Treat it as remote code execution by design. See [SECURITY.md](SECURITY.md).

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

export OPENROUTER_API_KEY=sk-or-...

python -m looper.cli --check-config                 # validate config.yaml
python -m looper.cli --goal "build a CLI todo app"  # one build, then exit
python -m looper.cli --daemon                       # 24/7: HTTP + file watcher
```

`python daemon.py --goal "..."` still works — `daemon.py` is a compatibility
shim over the `looper` package.

---

## How it works

```
goal ──> ┌─────────────── cycle 1 ────────────────┐
         │ research → architecture → build →      │
         │ test → review → security_audit         │
         └────────────────┬───────────────────────┘
                          │  score
              ┌───────────┴────────────┐
     score >= target?             score < min_acceptable?
              │                          │
             done                     fix ──> cycle 2..N
                                        (test/review/audit only)
                          │
              score >= min_acceptable
                          │
              performance_optimize → documentation
```

### The agents

| Phase | Agent | Produces |
|---|---|---|
| `research` | Researcher | `research.md` |
| `architecture` | Architect + UX/API Designer | `design.md` |
| `build` | Code Builder | `generated_code.py` |
| `test` | Test Generator | `test_generated.py`, then runs pytest |
| `review` | Senior Reviewer | `review.md` + a 0–100 score |
| `security_audit` | Security Auditor | `security_audit.md` + findings |
| `performance_optimize` | Optimizer | `optimized_code.py` |
| `documentation` | Doc Writer | `README_generated.md` |
| `fix` | Expert Fixer | patched code, promoted to canonical |

Phase lists are configurable — see the commented block in `config.yaml`.

---

## Scoring

Points are additive, then **hard caps** are applied. See
[ADR-002](docs/adr/002-fail-closed-scoring.md).

| Component | Max |
|---|---|
| Build succeeded | 20 |
| Tests passed (proportional) | 30 |
| Security (30 − weighted penalties) | 30 |
| Reviewer score | 20 |

Penalties per finding: `CRITICAL 30 · HIGH 15 · MEDIUM 5 · LOW 2`.

**Hard caps** — no amount of good news overrides these:

- No successful build, **or zero tests** → capped at **60**
- Any `CRITICAL` finding → capped at **50**

A failed security agent emits `CRITICAL: security audit did not complete`. An
outage never reads as "no issues found".

---

## Triggering builds

**File watcher** — write a goal into `looper_commands.txt`:

```bash
echo "build a URL shortener" >> looper_commands.txt
```

**HTTP API** (`--daemon` mode):

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/build` | token | Start a build: `{"goal": "..."}` |
| GET | `/status` | token | Full state: cycle, score, breakdown, history |
| GET | `/health` | none | Liveness; leaks nothing |
| GET | `/metrics` | token | Counters + uptime |

```bash
export LOOPER_HTTP_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
curl -X POST localhost:9999/build \
     -H "Authorization: Bearer $LOOPER_HTTP_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"goal":"build a URL shortener"}'
```

Protections: constant-time token comparison, per-IP rate limiting, request body
and goal length caps, and a startup **refusal** to bind `0.0.0.0` without a token.

---

## Configuration

Everything lives in `config.yaml`; all fields are optional. **Secrets never go
in this file** — it stores only the *names* of environment variables.

| Env var | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter credential (required for real runs) |
| `LOOPER_HTTP_TOKEN` | Bearer token for `/build`, `/status`, `/metrics` |

Validate before deploying:

```bash
python -m looper.cli --check-config
```

### CLI

| Flag | Effect |
|---|---|
| `--goal "..."` | Run one build, exit non-zero if below `min_acceptable` |
| `--daemon` | Run HTTP server + file watcher until signalled |
| `--check-config` | Validate config and exit |
| `--reset` | Clear persisted state |
| `--config PATH` | Use a specific config file |
| `--json-logs` | Structured JSON logs for log shippers |
| `--log-level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

Exit codes: `0` ok · `1` config error · `2` build below minimum.

---

## Layout

```
looper/
  config.py        frozen, validated config tree
  state.py         atomic writes, bounded history
  scoring.py       severity weighting + release gates
  testparse.py     pytest output parsing
  prompts.py       pure prompt templates
  llm.py           OpenRouter client, retries, backoff
  phases.py        pipeline stages, workspace containment
  server.py        HTTP control plane
  watcher.py       file-trigger polling
  orchestrator.py  the control loop
  cli.py           argv, logging, signals -- all side effects
daemon.py          compatibility shim
docs/adr/          architecture decision records
```

Importing any module has **no side effects** — no config read, no network
client, no file I/O. See [ADR-001](docs/adr/001-package-layout-and-immutable-config.md).

---

## Development

```bash
black --check looper/ tests/ daemon.py
isort --check-only looper/ tests/ daemon.py
flake8 looper/ tests/ daemon.py
mypy looper/ daemon.py --strict
pytest --cov=looper --cov=daemon --cov-branch --cov-fail-under=100
bandit -r looper/ daemon.py -ll
pip-audit -r requirements.txt --strict
```

Coverage is pinned at **100% line and branch**. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
