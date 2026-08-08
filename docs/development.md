# Development

- [Setup](#setup)
- [Quality gates](#quality-gates)
- [Architecture decision records](#architecture-decision-records)

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

---

## Quality gates

Every one of these runs in CI and must pass before a merge:

```bash
black --check looper/ tests/ daemon.py
isort --check-only looper/ tests/ daemon.py
flake8 looper/ tests/ daemon.py
mypy looper/ daemon.py --strict
pytest --cov=looper --cov=daemon --cov-branch --cov-fail-under=100
bandit -r looper/ daemon.py -ll
pip-audit -r requirements.txt --strict
python -m looper.cli --check-config
python -m looper.cli --check-models
```

Coverage is pinned at **100% line and branch**. This is not vanity: the
safeguards in this repo are mostly *refusals*, and an untested refusal is
indistinguishable from a missing one.

The CI workflow additionally runs `--check-models` on a **weekly cron**. Model
rot is time-based, not commit-based — a slug that works today can be retired
next month without anyone touching this repo.

See [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Architecture decision records

Every non-obvious design choice is recorded in [`adr/`](adr/):

| ADR | Decision |
|---|---|
| [001](adr/001-package-layout-and-immutable-config.md) | Package layout, side-effect-free imports, immutable config |
| [002](adr/002-fail-closed-scoring.md) | Fail-closed scoring with hard caps |
| [003](adr/003-timeouts-retry-classification-cost.md) | Timeouts, retry classification, cost tracking |
| [004](adr/004-verified-evidence.md) | HTTP control plane and its bind policy |
| [005](adr/005-cost-budget.md) | Hard USD budget per build |
| [006](adr/006-sandbox-and-anti-overfit.md) | Sandboxed execution and anti-overfitting |
| [007](adr/007-scope-guard.md) | Per-prompt scope guard |
| [008](adr/008-cross-platform-sandbox-fail-closed.md) | Cross-platform sandbox, fail-closed |
| [009](adr/009-multi-file-artifacts.md) | Multi-file artifacts |
| [010](adr/010-git-integration.md) | Git integration for reviewability |
