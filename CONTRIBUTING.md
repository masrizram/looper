# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
export OPENROUTER_API_KEY=sk-or-...                 # only needed for real runs
```

## The quality gate

Every one of these must pass before a PR is merged. CI runs the same commands.

```bash
black --check looper/ tests/ daemon.py
isort --check-only looper/ tests/ daemon.py
flake8 looper/ tests/ daemon.py
mypy looper/ daemon.py --strict
pytest --cov=looper --cov=daemon --cov-branch --cov-fail-under=100
bandit -r looper/ daemon.py -ll
pip-audit -r requirements.txt --strict
```

Coverage is pinned at **100% line and branch**. This is deliberate: the audit
that produced v2.0 found four critical bugs in a codebase sitting at 82%
coverage, including a security parser that could never match. Coverage is not
proof of correctness, but an uncovered line is a line nobody has ever run.

## House rules

1. **No side effects at import time.** Config loading, network clients, and
   file I/O belong in `main()` or a constructor. See `docs/adr/001`.
2. **Fail closed.** New `PhaseResult` fields default to the pessimistic value
   (`ok=False`, `build_ok=False`, `review_score=0`). A phase that forgets to
   set a field must never read as a success.
3. **Never swallow `CancelledError`.** Re-raise it before any broad
   `except Exception`, or the daemon becomes unkillable.
4. **Inject collaborators.** Every class takes its dependencies as constructor
   arguments so tests need no monkeypatching of globals.
5. **Tests must not touch the network, the real clock, or real subprocesses.**
   Use the fakes in `tests/conftest.py`.
6. **Every bug fix ships with a regression test** whose docstring names the
   defect it locks out.

## Adding a phase

1. Add `run_<name>` to `PhaseManager` returning a `PhaseResult`.
2. Register `<name>` in `KNOWN_PHASES` in `looper/config.py`.
3. Add it to the relevant list in `config.yaml`.
4. Add tests for the success path, the agent-failure path, and any parsing.

## Commit style

Conventional commits: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`.
