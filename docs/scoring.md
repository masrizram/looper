# Scoring & the release gate

What a score means, and why a build cannot talk its way past the gate.

Points are additive, then **hard caps** are applied. Full rationale in
[ADR-002](adr/002-fail-closed-scoring.md).

---

## Components

| Component | Max |
|---|---|
| Build succeeded | 20 |
| Tests passed (proportional) | 30 |
| Security (30 − weighted penalties) | 30 |
| Reviewer score | 20 |

Penalties per finding: `CRITICAL 30 · HIGH 15 · MEDIUM 5 · LOW 2`.

Severity is weighted rather than flat, because one CRITICAL scoring the same
as one LOW is not a security signal.

---

## Hard caps

No amount of good news overrides these:

- No successful build, **or zero tests** → capped at **60**
- Any `CRITICAL` finding → capped at **50**

---

## Fail-closed by construction

A failed security agent emits `CRITICAL: security audit did not complete`. An
outage therefore reads as *unverified*, never as "no issues found" — the
difference between a gate and a formality.

The same principle holds elsewhere: an unavailable sandbox refuses the test
run rather than executing unconfined, and a build whose code does not parse
cannot reach `done`.

---

## Gate outcomes

| Situation | Exit code |
|---|---|
| Score ≥ `min_acceptable` | `0` |
| Score < `min_acceptable` | `3` |
| Cost ceiling crossed mid-build | `4` |
| No isolation backend available | `5` |

These are meant to be consumed by CI — `looper --goal "..."` is usable directly
as a pipeline gate.
