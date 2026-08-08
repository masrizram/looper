# ADR-001: Package layout, immutable config, and no import-time side effects

**Status:** Accepted · **Date:** 2026-08-08 · **Supersedes:** the v1 single-file `daemon.py`

## Context

v1 was a single 1,058-line `daemon.py` that called `configure()` at module
scope. Three consequences:

1. `import daemon` read `config.yaml` from the current working directory and
   raised `FileNotFoundError` anywhere else. The module was un-importable from
   a REPL, a notebook, or any other tool.
2. Fifteen module-level globals (`CONFIG`, `WORKSPACE`, `AGENTS`, ...) meant
   two configurations could not coexist, and tests had to monkeypatch globals.
3. `PhaseManager` read those globals directly instead of receiving them, so
   the class could not be constructed in isolation.

## Decision

**Split into a package** with one responsibility per module:

| Module | Responsibility |
|---|---|
| `config.py` | Load, validate, and freeze configuration |
| `state.py` | Durable state; atomic writes; bounded growth |
| `scoring.py` | Severity-weighted scoring and release gates |
| `testparse.py` | Parse pytest output |
| `prompts.py` | Pure prompt templates |
| `llm.py` | OpenRouter client, retries, backoff |
| `phases.py` | Pipeline stages, workspace containment |
| `server.py` | HTTP control plane |
| `watcher.py` | File-trigger polling |
| `orchestrator.py` | The control loop |
| `cli.py` | Argument parsing, logging, signals — **all side effects** |

**Config is a frozen dataclass tree.** Validation happens once in
`__post_init__`; every consumer receives the object it needs via its
constructor. `daemon.py` remains as a thin re-export shim so existing scripts
and `python daemon.py` keep working.

## Consequences

*Positive.* Importing any module is free of side effects and testable from
anywhere. Two configs can coexist in one process. Every class is unit-testable
without patching globals, which is what made 100% branch coverage achievable.
`mypy --strict` passes because config fields have concrete types instead of
`Any` from a dict.

*Negative.* More files to navigate, and config changes now touch a dataclass
plus its validation rather than one dict key. Both are acceptable trades for
testability.

## Alternatives rejected

- **Pydantic settings** — a heavyweight dependency for what stdlib dataclasses
  already do; the validation here is simple and explicit.
- **Keep globals, add a `reset_config()` for tests** — leaves the coupling in
  place and makes tests order-dependent.
