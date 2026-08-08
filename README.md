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
| `architecture` | Architect + UX/API Designer | `architecture/design.md` |
| `build` | Code Builder | `generated_code.py` |
| `test` | Test Generator | `test_generated.py`, then runs pytest |
| `review` | Senior Reviewer | `review.md` + a 0–100 score |
| `security_audit` | Security Auditor | `security_audit.md` + findings |
| `performance_optimize` | Optimizer | `optimized_code.py` |
| `documentation` | Doc Writer | `docs/README.md` |
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

## Built-in safeguards

Looper executes code written by a language model, so it is hardened against
the failure modes of autonomous agents by design:

- **Cost ceiling (`max_cost_usd`).** Each build tracks estimated API spend
  (token usage × per-model price). Crossing the ceiling aborts the build
  *hard* and exits `4` — no silent bill runaway (ADR-005).
- **Untrusted-code sandbox (`sandbox_tests`).** Generated test suites run in a
  fixed-argv subprocess under OS resource limits (CPU/wall/RSS). A static scan
  *refuses to run* any suite that shells out, spawns processes, touches the
  network, or uses `eval`/`exec` — so dangerous LLM output is never executed
  on the host (ADR-006).
- **Anti-overfitting (`user_tests_dir` + `min_test_assertions_per_100_lines`).**
  Because the AI writes both the code *and* its tests, a weak suite cannot
  green-light a build: the suite must clear an assertion-density floor, must
  not hardcode the expected score, and — if you supply `user_tests_dir` — the
  generated code must also pass *your* tests, which the AI never sees (ADR-06).
- **Lint gate (`lint_generated`).** Generated code is `py_compile`/`flake8`
  checked before it is accepted, so output that does not even compile never
  reaches the "done" state.
- **Scope guard.** Every agent prompt injects a strict "stay within the goal,
  do not shell out, do not hallucinate changes to pass tests" directive, so a
  long loop cannot drift off the original task (ADR-007).
- **Loop cap (`max_cycles`).** A build always stops after N cycles; a human
  evaluates anything still failing (ADR-001).

### Safeguard configuration

All safeguards are configured under the `execution` key in `config.yaml`:

| Key | Default | Purpose |
| --- | --- | --- |
| `max_cost_usd` | `0.0` (off) | Hard USD ceiling per build; `0` disables the abort (ADR-005). |
| `model_prices_usd_per_1k` | `{}` | Per-model price overrides; falls back to `default_token_price_usd`. |
| `default_token_price_usd` | `0.002` | Used when a model has no explicit price. |
| `sandbox_tests` | `true` | Run generated suites in the resource-limited subprocess. |
| `sandbox_cpu_seconds` | `60` | POSIX CPU-time rlimit for one test run. |
| `sandbox_wall_seconds` | `300` | POSIX wall-time rlimit (Windows: covered by the pytest `timeout`). |
| `sandbox_rss_bytes` | `1_000_000_000` | POSIX address-space rlimit. |
| `min_test_assertions_per_100_lines` | `6` | Assertion-density floor; `0` disables it. |
| `user_tests_dir` | `""` (off) | Dir of human-owned tests the AI cannot see/edit. |
| `lint_generated` | `"py_compile"` | `off` \| `py_compile` \| `flake8` gate on generated code. |

The design rationale for each safeguard is recorded in `docs/adr/`:
ADR-005 (cost budget), ADR-006 (sandbox + anti-overfit), ADR-007 (scope guard).

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
| GET | `/metrics` | token | Counters, uptime, token spend |

```bash
export LOOPER_HTTP_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
curl -X POST localhost:9999/build \
     -H "Authorization: Bearer $LOOPER_HTTP_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"goal":"build a URL shortener"}'
```

Protections: constant-time token comparison, per-IP rate limiting, request body
and goal length caps, and a startup **refusal** to bind *any* non-loopback address
(`0.0.0.0`, `::`, or a LAN address) without a token.

### Resilience & cost control

Every LLM call has a hard timeout (`openrouter.request_timeout_seconds`), so a
stalled connection cannot wedge the daemon. Errors are **classified** before
retrying — a `401` fails after one attempt instead of burning the whole budget,
while `429` and `5xx` back off exponentially. Token usage is accumulated per
call and reported at `/status` and `/metrics`. Files written from agent output
are capped by `execution.max_file_bytes`. See
[ADR-003](docs/adr/003-timeouts-retry-classification-cost.md).

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
| `--version` | Print version and exit |
| `--goal "..."` | Run one build, exit non-zero if below `min_acceptable` |
| `--daemon` | Run HTTP server + file watcher until signalled |
| `--check-config` | Validate config and exit |
| `--reset` | Clear persisted state |
| `--config PATH` | Use a specific config file |
| `--json-logs` | Structured JSON logs for log shippers |
| `--log-level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

Exit codes: `0` ok · `2` config error · `3` build below minimum · `4` cost budget exceeded · `130` interrupted.

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
