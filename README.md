# Looper

> Looper is a **fail-closed release gate for AI-written code, built for CI** —
> not an interactive assistant. It enforces a hard USD ceiling, runs a
> human-owned test suite the model never sees, and exits with deterministic
> codes a pipeline can branch on.

Ten specialised LLM agents run in a scored loop — build → test → review →
audit → fix — but the point is not that an AI writes code. The point is that
Looper **refuses to accept it without evidence**.

```bash
# NOTE: the name `looper` on PyPI belongs to an unrelated project.
# Install from source:
git clone https://github.com/masrizram/looper && cd looper && pip install -e .

looper --doctor                            # what isolation does this host actually have?
looper --goal "build a URL shortener"      # exit 3 if it does not clear the gate
```

| Guarantee | Mechanism |
|---|---|
| Cannot silently overspend | `max_cost_usd` → abort, exit `4` ([ADR-005](docs/adr/005-cost-budget.md)) |
| Cannot green-light itself | assertion-density floor + `user_tests_dir` ([ADR-006](docs/adr/006-sandbox-and-anti-overfit.md)) |
| Cannot run unconfined | Docker/rlimit sandbox, **fail-closed**, exit `5` ([ADR-008](docs/adr/008-cross-platform-sandbox-fail-closed.md)) |
| Cannot fake a pass | hard score caps: no build/tests → 60, any CRITICAL → 50 ([ADR-002](docs/adr/002-fail-closed-scoring.md)) |
| Cannot escape review | one git branch + one commit per cycle ([ADR-010](docs/adr/010-git-integration.md)) |

> **Read this first:** `POST /build` runs code written by a language model.
> Treat it as remote code execution by design. See [SECURITY.md](SECURITY.md).

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

export OPENROUTER_API_KEY=sk-or-...   # bash / Linux / macOS
# Windows (PowerShell):  $env:OPENROUTER_API_KEY="sk-or-..."

looper --check-config                 # validate config.yaml
looper --check-models                 # every model slug really exists
looper --doctor                       # can this host isolate generated code?
looper --goal "build a CLI todo app"  # one build, then exit
looper --resume --goal "..."          # same goal: skip phases already paid for
looper --daemon                       # 24/7: HTTP + file watcher
```

New here? Start with [`examples/01-minimal-trial`](examples/01-minimal-trial/) —
three phases, one cycle, a hard $0.50 ceiling.

**No Docker on Windows?** `--doctor` exits `5` and builds refuse to run
generated tests; that is the fail-closed contract, not a bug. Either install
Docker, or run `wsl --install` — WSL is now a supported sandbox backend
([ADR-016](docs/adr/016-wsl-as-a-third-sandbox-backend.md)).

`python daemon.py --goal "..."` still works — `daemon.py` is a compatibility
shim over the `looper` package.

---

## Documentation

| Guide | What is in it |
|---|---|
| [Architecture](docs/architecture.md) | The build loop, the ten agents, model tiering, package layout |
| [Scoring](docs/scoring.md) | Score components, hard caps, why the gate is fail-closed |
| [Safeguards](docs/safeguards.md) | Cost ceiling, sandbox, anti-overfitting — and every `execution` key |
| [Artifacts & git](docs/artifacts.md) | Multi-file output, path allowlist, per-cycle commit trail |
| [Operations](docs/operations.md) | Triggering builds, HTTP API, retry/timeout behaviour |
| [Configuration & CLI](docs/configuration.md) | Config file, env vars, every flag, every exit code |
| [Example prompts](docs/example-prompts.md) | 3 copy-paste prompts that exercise the strongest features |
| [Examples](examples/) | 5 runnable scenarios: minimal trial, budget guard, human-owned tests, CI gate, daemon + webhook |
| [Development](docs/development.md) | Quality gates, 100% coverage policy, ADR index |

Design decisions live in [`docs/adr/`](docs/adr/) — sixteen records covering why
each safeguard exists.

---

## Contributing

Quality gates are strict and non-negotiable: `black`, `isort`, `flake8`,
`mypy --strict`, `bandit`, `pip-audit`, and **100% line and branch coverage**.
The safeguards here are mostly *refusals*, and an untested refusal is
indistinguishable from a missing one. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [Development](docs/development.md).

## License

MIT — see [LICENSE](LICENSE).
