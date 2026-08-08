# ADR-011: Split `phases.py` by responsibility (SRP)

- Status: accepted
- Date: 2026-08-08
- Decided after: v3 code audit (architectural finding A-3)

## Context

`looper/phases.py` had grown to 750 lines carrying five distinct
responsibilities in one file:

1. result/evidence data types (`PhaseResult`, `CycleEvidence`),
2. filesystem writes and path containment (`write_file`, `resolve_in_workspace`),
3. subprocess execution of untrusted code (`_run_pytest`, `_lint_generated`),
4. the LLM-agent orchestration template (`_run_agent_phase`),
5. the nine pipeline stage methods.

That is the same single-responsibility violation the rest of the package was
built to avoid, and it made the trust boundary hard to see: the code that
*runs untrusted output* sat in the same file as the code that *asks an agent a
question*. Reviewers had to scroll past subprocess handling to reason about
agent logic. The v3 audit rated this a clear architectural weakness.

## Decision

Split `phases.py` into a `looper/phases/` package with one file per
responsibility:

- `phases/results.py`     -- data contract only (`PhaseResult`, `CycleEvidence`, `replace_result`)
- `phases/workspace.py`   -- the only filesystem sink; path containment + `strip_code_fences`
- `phases/execution.py`   -- everything that runs untrusted code (syntax, lint, sandboxed pytest)
- `phases/agents.py`      -- the nine pipeline stages, on top of one template method
- `phases/__init__.py`    -- `PhaseManager` (composes three mixins) + re-exports every public name

`PhaseManager` is now `WorkspaceMixin, ExecutionMixin, AgentPhasesMixin` (MRO
order chosen so the real `resolve_in_workspace`/`write_file` win over the
contract stubs other mixins declare).

**Public API unchanged.** Every name previously importable from
`looper.phases` -- `PhaseManager`, `PhaseResult`, `CycleEvidence`, `CODE_FILE`,
`strip_code_fences`, `run_sandboxed`, `scan_for_dangerous_calls`,
`WorkspaceEscapeError`, `replace_result` -- is still importable from there,
because `__init__.py` re-exports it. `monkeypatch.setattr("looper.phases.run_sandboxed", ...)`
still resolves (it now points at the re-export, which aliases the real
`sandbox.run_sandboxed`).

## Consequences

- Positive: the `execution.py` byte-count is the only place untrusted code
  runs, so reviewers audit the trust boundary in one file. The 750-line file is
  gone (responsibilities now 103/105/221/411 lines).
- Positive: no behaviour change -- the refactor is verified by the existing
  581-test suite passing unchanged (only the `run_sandboxed`-patch test target
  moved to `looper.phases.execution`).
- Positive: new `scripts/sandbox_integration_test.py` (ADR-010 follow-on,
  wired as the `sandbox-integration` CI job) now has a clean module to assert
  against.
- Negative: one more layer of indirection (mixins). The contract stubs in
  `agents.py`/`execution.py` exist only so `mypy --strict` resolves the cross-
  mixin calls; they raise `NotImplementedError` and are never reached.
- Negative: `pytest` module-isolation (`importmode=importlib`) means the
  package layout must stay as a directory, not a flattened module.

## Alternatives considered

- *Keep the file, just shrink it.* Rejected: the mixing of "ask an agent" and
  "run hostile code" in one file is the actual problem, not the line count.
- *Subclass instead of mixins.* Rejected: would have broken the public
  `PhaseManager` name and every existing import site. The mixin split keeps the
  constructor and the public surface byte-identical.
