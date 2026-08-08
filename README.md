# Looper

**A release gate for AI-written code.** Ten specialised LLM agents run in a
scored loop — build → test → review → audit → fix — but the point is not that
an AI writes code. The point is that Looper **refuses to accept it without
evidence**: fail-closed scoring, a hard USD budget, sandboxed test execution,
and human-owned tests the AI never sees.

```bash
pip install looper
looper --doctor                            # what isolation does this host actually have?
looper --goal "build a URL shortener"      # exit 3 if it does not clear the gate
```

| Guarantee | Mechanism |
|---|---|
| Cannot silently overspend | `max_cost_usd` → abort, exit `4` (ADR-005) |
| Cannot green-light itself | assertion-density floor + `user_tests_dir` (ADR-006) |
| Cannot run unconfined | Docker/rlimit sandbox, **fail-closed**, exit `5` (ADR-008) |
| Cannot fake a pass | hard score caps: no build/tests → 60, any CRITICAL → 50 (ADR-002) |
| Cannot escape review | one git branch + one commit per cycle (ADR-010) |

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
- **Untrusted-code sandbox, fail-closed (`sandbox_backend`).** Generated test
  suites run either in a throwaway Docker container (read-only, `--network=none`,
  cpu/memory/pids capped, no-new-privileges) or under POSIX rlimits. A static
  scan *refuses to run* any suite that shells out, spawns processes, touches
  the network, or uses `eval`/`exec`. **If no isolation backend is available,
  the suite is refused rather than run unconfined** — previously it silently
  degraded to no sandbox on Windows (ADR-006, ADR-008). Check your host with
  `looper --doctor`.
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
| `sandbox_backend` | `auto` | `auto` \| `rlimit` \| `docker` \| `none`. `auto` prefers Docker (ADR-008). |
| `sandbox_image` | `python:3.11-slim` | Image used by the Docker backend. |
| `sandbox_network` | `none` | Container network mode. |
| `sandbox_fail_closed` | `true` | Refuse to run generated tests when no isolation exists. |
| `artifact_mode` | `single_file` | `single_file` \| `package` — multi-file output (ADR-009). |
| `max_files_per_build` | `25` | Cap on files a package build may write. |
| `git.enabled` | `false` | Commit each cycle to a branch for review (ADR-010). |
| `min_test_assertions_per_100_lines` | `6` | Assertion-density floor; `0` disables it. |
| `user_tests_dir` | `""` (off) | Dir of human-owned tests the AI cannot see/edit. |
| `lint_generated` | `"py_compile"` | `off` \| `py_compile` \| `flake8` gate on generated code. |

The design rationale for each safeguard is recorded in `docs/adr/`:
ADR-005 (cost budget), ADR-006 (sandbox + anti-overfit), ADR-007 (scope guard).

---

## Multi-file artifacts

By default a build produces one `src/generated_code.py`. Set
`execution.artifact_mode: package` and the builder may emit a file tree using
an explicit marker:

```
### FILE: src/app/models.py
```python
class User: ...
```
```

Paths go through an allowlist (relative only, no `..`, no drive letters, known
extensions), the file count is capped by `max_files_per_build`, **every**
`.py` file must parse and pass the lint gate, and the reviewer and security
agents receive all modules concatenated — auditing only the first file would
let vulnerabilities in the rest through. No markers in the reply means the
build falls back to single-file, so enabling package mode can never break a
working build. See [ADR-009](docs/adr/009-multi-file-artifacts.md).

---

## Git integration

```yaml
execution:
  git:
    enabled: true
    branch_prefix: "looper/"
    commit_per_cycle: true
```

Each build checks out `looper/<slug-of-goal>` and commits once per cycle with
the score breakdown in the message, so `git log --oneline` is the build's audit
trail and `git diff HEAD~1` shows exactly what a fix cycle changed. The goal is
attacker-influenced text, so it is slugified through an allowlist before it can
reach a git ref. Every git failure is non-fatal — version control here is
observability, not correctness. See [ADR-010](docs/adr/010-git-integration.md).

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
| `--check-models` | Verify every configured model slug against the live OpenRouter catalogue, and exit |
| `--doctor` | Report the sandbox/git capability this host actually has, and exit |
| `--reset` | Clear persisted state |
| `--config PATH` | Use a specific config file |
| `--json-logs` | Structured JSON logs for log shippers |
| `--log-level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

Exit codes: `0` ok · `2` config error · `3` build below minimum · `4` cost budget exceeded · `5` sandbox unavailable · `130` interrupted.

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
  artifact.py      multi-file artifact parsing, path allowlist
  sandbox.py       backend resolution, container/rlimit isolation
  vcs.py           git branch + per-cycle commits
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
