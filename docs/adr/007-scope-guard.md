# ADR-007: Per-prompt scope guard against context drift

## Status
Accepted (2026-08-08)

## Context
In a multi-cycle loop the "conversation" is file-based, not a growing chat log,
which already limits prompt drift. But long runs still risk agents expanding
scope: adding features, touching unrelated files, or hallucinating changes just
to make a test pass.

## Decision
Inject a shared `SCOPE_GUARD` directive into every agent prompt
(`looper.prompts`). It instructs the agent to stay strictly within the stated
goal, not to add features, refactor unrelated code, install packages, run shell
commands, or touch files outside its phase, and not to hallucinate changes to
satisfy a test. The QA prompt additionally forbids hardcoding expected results.

## Consequences
- Each agent is re-grounded in the original goal every call.
- Reduces scope creep and the "fake a pass" failure mode; it is a guardrail,
  not a cryptographic guarantee, so it complements (not replaces) the
  sandbox/adequacy gates in ADR-006.

## Verification
`tests/test_phases.py::test_scope_guard_injected_into_every_prompt` asserts the
directive is present in all nine agent prompts.
