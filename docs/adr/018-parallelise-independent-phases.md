# ADR-018: Parallelise only phases that read nothing their co-runner writes

Status: Accepted

## Context

The build loop ran all ten agents strictly serially: five cycles, each a
chain of research, architecture, build, test, review, security_audit, fix.
Wall-clock per cycle was the sum of every agent round-trip, and frontier
models are slow. The benchmark audit scored developer experience 11/20, and
latency was a named component of that.

Most of that ordering is real. `build` reads the architecture document,
`test` reads the built artifact, `fix` reads the review findings. Reordering
any of those produces a review of code the builder has not finished writing.

Two phases are different. `review` and `security_audit` both read the
*finished* artifact, write to separate files, and contribute independent
terms to the score. Neither reads the other's output. They were costing a
full agent round-trip of wall clock for no ordering benefit whatsoever.

## Decision

Introduce `PARALLEL_PHASE_GROUP = {"review", "security_audit"}` and run
*adjacent* members of that set concurrently via `asyncio.gather`; every other
phase remains a batch of one.

Three constraints make this safe:

**A small allowlist, not a dependency solver.** A solver infers edges from
declarations that drift out of step with the code. Membership in this set is
an explicit claim -- "this phase reads nothing its co-runner writes" -- that a
human makes deliberately when adding a phase.

**Only adjacent phases group.** `_parallel_batches` collapses consecutive
members and preserves order otherwise, so reordering the configured phase
list can never silently hoist a phase past a dependency.

**Budget is reserved, not merely checked.** ADR-013 fixed the cost ceiling by
reserving projected worst-case cost *before* sending a request. That
reservation is what makes concurrency safe here: two phases firing at once
each reserve before their call, so the ceiling holds under `gather` exactly
as it does serially. Had the check remained a pre-flight comparison with the
charge applied afterwards, two concurrent calls could both observe headroom
that only one of them actually had, and the ADR-013 fix would have been
quietly undone by this ADR.

## Consequences

Good: one agent round-trip removed from every cycle -- up to five per build --
with no change to what is scored or to the evidence rules of ADR-004.

Bad: interleaved log lines from two agents. Mitigated by logging the batch
explicitly (`Phases (parallel): review, security_audit`) so the interleaving
is expected rather than alarming. Failure semantics also widen slightly: both
phases run to completion even when the first to finish already failed.

Neutral: the group is currently a pair. Nothing about the mechanism assumes
that, but nothing else in the current phase list qualifies.

## Alternatives rejected

**A general dependency graph over phases.** More machinery, and the failure
mode is worse: a mis-declared edge produces a plausible-looking build with
evidence gathered in the wrong order, which the score cannot detect.

**Parallelise across cycles.** Cycle N+1 consumes cycle N's fixes; there is
no independence to exploit.

**Parallelise the model calls inside a phase.** `run_architecture` already
fires an architect and a designer. They are genuinely sequential -- the
designer reads the architect's output -- and a 402 from the architect
short-circuits the designer entirely.
