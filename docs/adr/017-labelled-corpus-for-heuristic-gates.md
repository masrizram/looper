# ADR-017: A labelled corpus, not coverage, calibrates the heuristic gates

Status: Accepted

## Context

ADR-014 recorded the lesson from the v5 audit: coverage proves a line
*executed*, never that it was *correct*. Every defect that audit found sat
under a green suite at 100% line and branch coverage.

The lesson was recorded but not mechanised. Two audit rounds later the same
shape kept reappearing in the heuristic gates -- `adequacy.py`,
`sandbox.scan_for_dangerous_calls`, `scoring.reports_no_issues` -- because
each of those gates is a *classifier*, and a classifier cannot be validated
by executing its branches. Measured examples from the audits:

- `assert cart.total == 10` was **refused** as a hardcoded verdict, so an
  idiomatic shopping-cart build could never reach `target_score`.
- `assert 1 == 1` repeated three times was **accepted** by adding
  `import logging`.
- A docstring reading "never use socket." tripped the dangerous-call scan.
- `make_file(tmp_path).write_text(...)` -- ordinary pytest temp-file I/O --
  was refused.

Each was fixed individually. Nothing stopped the next one, so the next one
was always found the same way: by accident.

A false positive costs as much as a miss. A gate that refuses every
legitimate build blocks adoption exactly as effectively as a gate that waves
bad code through blocks safety.

## Decision

Every heuristic gate is measured against a **labelled corpus** held in
`tests/corpus/`, written from outside the implementation, and scored with
`looper.calibration.evaluate_gate` into a confusion matrix.

`tests/test_gate_calibration.py` asserts floors on `recall` and `precision`
and a ceiling on `false_positive_rate` for each gate, and additionally
asserts that each corpus carries at least four positives **and** four
negatives -- a corpus of only-positives would satisfy any recall floor
vacuously.

`calibration.py` lives in the package, not the test tree, because the
numbers are a product property: Looper's claim is that its gates are
calibrated in both directions, and this module is the arithmetic behind
that claim.

Corpus samples state the *expected verdict* alongside the input, so a
sample is a specification rather than a snapshot of current behaviour. When
a threshold fails, the question is which of the two -- gate or label -- is
wrong; it is never resolved by editing the number down.

## Consequences

Good: a recalibration that fixes one direction and breaks the other now
fails CI instead of shipping and being found by the next audit. The
confusion matrix is reported on failure (`metrics.as_dict()`), so a
regression names its own precision and recall.

Bad: adding a heuristic now costs corpus samples in both directions, not
just a passing unit test. That is the intended price. The corpus is
hand-labelled and therefore finite -- it bounds regression, it does not
prove correctness on unseen input.

## Alternatives rejected

**Keep raising coverage.** Already at 100% line and branch when every one of
these defects shipped. There is no coverage number that catches a
miscalibrated threshold.

**Property-based testing (Hypothesis) instead of a corpus.** Generating
Python that is *realistically* adequate or dangerous is harder than the gate
itself, and a generator written by the same author repeats the same blind
spots. Hand-labelled real-shaped samples encode judgement a generator cannot.

**Assert on exact scores rather than rates.** Brittle: one added sample
shifts every expected number, and the pressure is then to update constants
rather than to investigate.
