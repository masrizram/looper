# ADR-014: Gates must be measured in both directions

Status: accepted
Date: 2026-08-09
Extends ADR-012 (calibrate heuristics both directions).

## Context

The v5 audit found five heuristic gates that were wrong, and the pattern was
the same every time: each had been tested in the direction it was written for
and never in the other. A gate has two failure modes and they cost differently
but they both cost.

What was measured:

| Gate | Miss (let bad through) | False positive (refuse good) |
| --- | --- | --- |
| `_SCORE_HARDCODE_RE` | — | `assert cart.total == 10` rejected as a hardcoded verdict, so no domain with a `total`/`score` attribute could ever reach `target_score` |
| `_imports_subject` | `assert 1 == 1` passed by writing `import logging` | — |
| `scan_for_dangerous_calls` | `import os as o; o.system(...)`, `from os import system`, `open(p, "w")` all reached the host | `make_file(tmp_path).write_text(...)` refused; any identifier containing `ctypes` refused |
| `reports_no_issues` | "Are there no vulnerabilities? Absolutely not, I found 4 criticals." read as a clean audit | — |
| `REVIEW_SCORE_RE` | — | a markdown table verdict scored 0, costing a full extra fix cycle |

Every one of these lived under 100% line and branch coverage. Coverage proves
a line executed; it says nothing about whether the line was right. The tests
that existed asserted the behaviour the author intended, using inputs the
author had in mind — which is precisely the input distribution under which the
bug is invisible.

## Decision

Any heuristic gate — a regex, a denylist, an AST tripwire, a threshold — must
ship with parametrized tests covering **both** directions, using inputs drawn
from outside the implementation:

1. **True positives**: the thing the gate exists to catch, in every spelling
   the language permits. For a call tripwire that means the direct call, the
   aliased import, the `from`-import, and the `getattr` indirection — not just
   the literal form the author typed while writing the pattern.
2. **True negatives**: realistic, legitimate code that superficially resembles
   a hit. `cart.total`, `my_ctypes_helper`, `open(p).read()`, a suite that
   reads its artifact from disk instead of importing it.

Where a gate compares against a table of names, the comparison must happen on
a *resolved* value, not on source text. The sandbox scanner now resolves import
aliases at their binding site before matching, which is what makes the
dotted-path table mean what it says.

## Consequences

Positive:

- The bidirectional test tables are executable documentation of what each gate
  considers in and out of scope, in a form a reviewer can extend in one line.
- False positives are treated as defects with a real cost, not as acceptable
  strictness. A gate that refuses every shopping-cart build is broken.

Negative:

- More test code per gate, and the tables need extending whenever a gate's
  scope changes. This is the intended cost: the alternative is a gate whose
  behaviour nobody has measured.

## Verification

`tests/test_audit_v5_regressions.py` carries the tables: 11 sandbox spellings
(6 refused, 5 allowed), 9 adequacy assertions (5 flagged, 4 not), 5 verdict
formats (3 parsed, 2 correctly ignored), and 3 audit-prose cases. Each one
failed before its fix.
