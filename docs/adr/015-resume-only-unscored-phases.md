# ADR-015: A resume may skip only unscored phases

Status: Accepted

## Context

A build that dies at cycle 1 -- a cancelled CI job, a 402, a laptop closing --
had exactly one recovery: run it again from phase 1 and pay for every agent a
second time. `research` and `architecture` are the two most expensive calls in
the pipeline (both on the frontier tier) and they run before anything of value
has been produced, so an interruption anywhere later re-bought them at full
price.

The state file already recorded `current_phase`, but only as a *report*. There
was no recovery point: nothing recorded which phases had genuinely finished,
and `reset()` at the start of every build wiped the file before anything could
read it.

The naive fix -- "skip whatever the state file says finished" -- is unsafe.
ADR-004 exists because a cycle may only score evidence it re-verified: if a
resume skipped `review`, cycle 1 would inherit a review score of 92 that this
run never earned, which is precisely the hole `CycleEvidence.invalidate_unverified`
was built to close. A resume that can restore a score is not a resume, it is a
way to launder a stale gate result.

## Decision

Resume is **opt-in** (`--resume`, default off) and may skip only phases in
`RESUMABLE_PHASES = {"research", "architecture"}` -- the phases that produce
*input* for later work and contribute **zero** score components.

A phase is skipped only when all of these hold:

1. `--resume` was passed;
2. the saved `current_goal` is byte-identical to the requested goal;
3. the saved run did not already complete (`status != "done"`);
4. the phase appears in the persisted `completed_phases` list, which is
   appended to only after a phase returns `ok=True`;
5. the phase's artifact still exists on disk **and is non-empty**.

The checkpoint is computed *before* `reset()` -- that ordering is the feature,
since `reset()` is what keeps each build independent -- and re-recorded
afterwards so a second interruption can resume too. A skip is consumed once,
by the cycle that inherited it: later cycles re-run the phase normally, because
`retry_phases` may legitimately want it.

## Consequences

Good: an interrupted build resumes without re-buying its two most expensive
calls. Every scored gate still runs in full, so the score keeps meaning what it
meant before. A wiped workspace, an edited goal, or a truncated artifact each
independently cancel the skip and cost nothing but a log line.

Bad: recovery is partial by design -- `build` and everything after it always
re-runs, so resume saves money, not time-to-green. `completed_phases` adds a
field to the state file (defaulted on load, so old files keep working).

## Alternatives rejected

**Resume every completed phase.** Fastest, and it silently violates ADR-004 by
restoring unverified scores.

**Snapshot and restore the whole `CycleEvidence` object.** Same defect with
extra machinery: the evidence would be trusted without re-verification.

**Resume by default.** A stale state file beside a hand-edited workspace would
then change behaviour with no flag in the command line. Recovery should be
something you asked for.
