# ADR-002: Fail-closed scoring with hard release gates

**Status:** Accepted · **Date:** 2026-08-08

## Context

The v1 scorer awarded points additively:

```python
score = 0
if build_ok:            score += 20
score += 30 * (tests_passed / tests_total) if tests_total else 0
score += max(0, 30 - len(security_issues) * 5)   # <- flat, and floors at 6
score += 20 * (review_score / 100)
```

Three defects, each confirmed by execution before the fix:

1. **A build that failed everything scored 30/100, not 0.** With no build, no
   tests, and no review, an *empty* `security_issues` list still handed out the
   full +30 security allowance. Absence of evidence was scored as evidence of
   safety.
2. **Severity was ignored.** One `CRITICAL: remote code execution` cost exactly
   as much as one `LOW: verbose error message` — 5 points each.
3. **The penalty saturated.** Six findings and seventy findings both scored
   70.0, because `max(0, 30 - n*5)` floors at zero once `n >= 6`.

The security list was itself always wrong: the parsing regex contained a
literal `\\s` inside a raw string and could never match (see the v2.0 audit),
so every audit silently produced zero real findings.

## Decision

**Weight penalties by severity**, configurable under `scoring.severity`:

```
CRITICAL 30 · HIGH 15 · MEDIUM 5 · LOW 2 · unrecognised 5
```

**Apply hard caps after the additive total**, because some conditions must
bound the outcome no matter how well everything else scored:

| Condition | Cap |
|---|---|
| `not build_ok` **or** `tests_total == 0` | 60 |
| any `CRITICAL` finding | 50 |

**Treat a failed agent as a blocking finding, never as silence.** If the
security auditor errors out, the phase emits
`CRITICAL: security audit did not complete`. An outage must not read as a
clean bill of health.

Applied caps are recorded in `ScoreBreakdown.caps_applied` and persisted to
state, so an operator can see *why* a build was held back.

## Consequences

*Positive.* The score now tracks verified evidence. A totally failed build
lands at 30 with an `unverified_build` cap, and no amount of reviewer optimism
can push an unbuilt, untested artifact into the release band.

*Negative.* Scores are lower than under v1 for the same artifact, and two v1
tests that asserted the buggy behaviour had to be rewritten. This is the point:
those tests were locking in the defect.

## Alternatives rejected

- **Multiplicative penalties** — harder to explain to an operator, and a single
  finding can annihilate an otherwise good score.
- **Refusing to score at all when the build fails** — loses the diagnostic
  signal about which stages did work.
