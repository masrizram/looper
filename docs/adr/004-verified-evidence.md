# ADR-004: Verified evidence — syntax gate, loopback rule, cycle invalidation

Status: Accepted
Date: 2026-08-08

## Context

A post-v2.0 audit re-ran the whole suite against the shipped code. Every gate
was green — `flake8` 0, `mypy --strict` clean, `bandit -ll` clean, 100% line
*and* branch coverage, `pip-audit` clean — and four real defects were still
present. All four are the same disease the project already documented in
ADR-002: **absence of evidence being scored as evidence of success.** The prior
green gates did not catch them because no test simulated the adversarial input,
and one test actively codified one of the bugs as expected behaviour.

Each was reproduced by execution before being fixed.

### S-2 — only `0.0.0.0` was treated as a public bind

`HTTPConfig.__post_init__` refused `0.0.0.0` without a token, but any other
non-loopback address merely logged `Unusual http.bind` and started anyway.

```
PROBE-1 bind='192.168.1.50' ACCEPTED without token, is_public=True
PROBE-1 bind='::'           ACCEPTED without token, is_public=True
PROBE-2 unauth /build on LAN bind -> (200, {'status': 'started', 'goal': 'x'})
```

`/build` is remote code execution by design, so this shipped an unauthenticated
RCE endpoint to anyone who set `bind` to the host's LAN address or to the IPv6
wildcard `::`. `is_public` already had the correct definition (`bind not in
LOOPBACK_BINDS`); the guard simply did not use it.

### S-3 — `build_ok` meant "the LLM replied", not "the code compiles"

`run_build` set `build_ok=reply.ok`. Any non-empty response — an apology, prose,
a truncated file — earned the full 20-point build weight *and* satisfied the
`unverified_build_cap` gate.

```
PROBE-4 score for 'LLM replied' build with zero compile check:
  {'build': 20.0, 'tests': 30.0, 'security': 30.0, 'review': 20.0, 'total': 100.0, 'caps_applied': []}
```

### S-4 — retry cycles re-banked stale evidence

`CycleEvidence` persists across cycles. A retry cycle whose `retry_phases` omits
`review` or `security_audit` kept the previous cycle's numbers and scored them
as if freshly verified.

```
PROBE-5 cycle1 issues: ['HIGH: sqli'] review: 98.0
PROBE-5 cycle2 reuses stale review/security: 98.0 ['HIGH: sqli']
```

The inverse is the dangerous direction: a clean cycle-1 audit (empty findings,
review 98) would be re-counted every subsequent cycle even after the fixer had
rewritten the code those findings were about.

### S-5 — background build crashes were silent

`_track` never inspected the task's exception. A crashing build returned HTTP
200 and then disappeared into asyncio's "Task exception was never retrieved"
stderr noise, with nothing in the daemon's own logs.

## Decision

1. **Any non-loopback bind requires a token.** The special case for `0.0.0.0`
   is gone; the check is `if self.bind not in LOOPBACK_BINDS`, the same
   predicate as `is_public`. One definition of "public", used everywhere.
2. **`build_ok` requires `ast.parse` to succeed** on the generated source
   (`PhaseManager._verify_syntax`), with markdown fences stripped first since
   agents habitually wrap code in them. Empty output and syntax errors fail
   closed and record a state error. The same check gates `run_fix`.
3. **`CycleEvidence.invalidate_unverified(phases)`** runs at the top of every
   cycle after the first. Evidence no phase in this cycle will re-establish is
   dropped: review → 0.0, tests → 0/0, and security → a blocking
   `MEDIUM: security audit not re-run this cycle` finding rather than an empty
   list (an empty list would score full marks).
4. **Background task failures are logged** via a `_log_task_failure` done
   callback, which skips cancellations so a clean shutdown stays quiet.

## Consequences

Good: the score now means something closer to what it claims. A build that does
not parse cannot reach the release band, a LAN bind cannot ship unauthenticated
RCE, a trimmed `retry_phases` degrades the score instead of inflating it, and a
dead build is visible in the daemon log.

Bad: `ast.parse` is Python-specific. A goal that asks the builder for Go or
TypeScript will now fail the syntax gate. That is deliberate for now — the
generated test phase already assumes pytest — but it makes the language
assumption explicit rather than implicit, and multi-language support will need
a per-language verifier.

Also bad: `invalidate_unverified` makes short `retry_phases` configurations
score lower than they used to. That is the point; the previous number was not
earned.

## Alternatives rejected

- *Compile with `py_compile` in a subprocess* — slower, and writes bytecode.
  `ast.parse` answers exactly the question asked: does this parse?
- *Warn on a LAN bind instead of refusing* — that is precisely what the code did
  and it was wrong. A warning does not stop the process, and the endpoint
  executes model-authored code.
- *Carry stale evidence but mark it stale in the breakdown* — more machinery for
  a worse guarantee. Fail closed instead.
