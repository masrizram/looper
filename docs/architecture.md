# Architecture

How a goal becomes an artifact, which agent owns which phase, and where each
module lives.

- [The build loop](#the-build-loop)
- [The agents](#the-agents)
- [Package layout](#package-layout)

---

## The build loop

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

Retry cycles deliberately re-run only the *validating* phases. Re-running
research and architecture on every fix would burn budget re-deriving decisions
that have not changed.

A build always stops after `max_cycles`; anything still failing is handed to a
human rather than looped on indefinitely
([ADR-001](adr/001-package-layout-and-immutable-config.md)).

---

## The agents

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

### Model tiering

Models are assigned by cost/capability, not brand. Two assignments are
deliberate and should not be "simplified":

* **`tester` uses a frontier model from a different family than `builder`.**
  Test design is where self-overfitting is caught. If the code's author and its
  test author share a family, they share blind spots.
* **`reviewer` is likewise off the builder's family** — a reviewer from the
  author's family grades its own prose more than it reviews it.

Verify every configured slug against the live catalogue before deploying:

```bash
looper --check-models
```

A wrong slug is well-formed YAML, so `--check-config` cannot catch it; without
this check it surfaces mid-pipeline, after earlier phases have already been
billed.

---

## Package layout

```
looper/
  config.py        frozen, validated config tree
  state.py         atomic writes, bounded history
  scoring.py       severity weighting + release gates
  testparse.py     pytest output parsing
  prompts.py       pure prompt templates
  llm.py           OpenRouter client, retries, backoff
  models.py        model-slug verification against the catalogue
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
client, no file I/O. See
[ADR-001](adr/001-package-layout-and-immutable-config.md).
