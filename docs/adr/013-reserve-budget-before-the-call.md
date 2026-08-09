# ADR-013: Reserve the budget before the call, not after it

Status: accepted
Date: 2026-08-09
Supersedes nothing. Extends ADR-005 (cost budget).

## Context

ADR-005 introduced `max_cost_usd` and described it as a hard ceiling. It was
not one.

`OpenRouterClient.call` checked the budget *before* issuing a request and
added the call's cost *after* the response came back. Those are the only two
points where cost is known, so the ceiling could only ever be observed once it
had already been breached: the check said "we are at $0.00 of $1.00, proceed",
the call returned, and $15.00 of Opus tokens landed in the ledger. The next
call was refused — one call too late.

The v5 audit measured this rather than reading it. With a $1.00 budget and a
single 60k-prompt / 8k-completion Opus call, final spend was $15.00, an
overshoot of 15x. The 100%-branch-covered test suite did not catch it because
every line of the budget code *ran*; nothing asserted the invariant the
documentation claimed.

An unattended daemon is exactly the setting where this matters. The budget
exists so a runaway loop cannot empty an account overnight, and a ceiling that
is enforced one call late is not a ceiling — it is a report.

## Decision

Project the worst-case cost of a call and reserve it *before* the request is
sent.

The projection is `(prompt_tokens_estimate + agent.max_tokens) / 1000 * price`,
where `prompt_tokens_estimate` is `len(prompt) / CHARS_PER_TOKEN`. Both terms
are deliberately pessimistic:

- `max_tokens` is the ceiling the provider is instructed to respect, so no
  response can cost more in completion tokens than we reserved;
- character-based prompt estimation errs high for code and English alike.

A call whose projection would push the running total past `max_cost_usd` is
refused with `CostBudgetExceeded` before any network I/O happens. Actual cost
still replaces the projection once the response arrives, so the ledger stays
accurate and an over-projected call does not permanently consume budget.

We also split completion pricing (`completion_prices_usd_per_1k`) from prompt
pricing, because providers bill output at 3-5x input. It defaults to empty:
the existing `model_prices_usd_per_1k` values are already blended input/output
figures, and inventing a multiplier on top of a blended number would make the
estimate worse, not better. Operators who know their real split can set it and
get exact accounting.

## Consequences

Positive:

- `max_cost_usd` is now a ceiling in the sense the README claims. Measured
  overshoot across $0.50 / $1.00 / $5.00 budgets is $0.0000.
- The refusal is free: it costs no tokens, because nothing is sent.
- Cost accounting is optionally exact rather than only approximate.

Negative:

- The projection is conservative, so a call that *would* have fit can be
  refused near the ceiling. This is the correct direction for a safety limit,
  and the alternative — admitting it and finding out afterwards — is the bug
  this ADR fixes.
- `CHARS_PER_TOKEN` is a heuristic. It is used only for the reservation, never
  for billing, so a bad estimate changes *when* we refuse, not what we report.

## Verification

`tests/test_audit_v5_regressions.py` asserts the invariant directly:
spend never exceeds the budget across three budget sizes, the expensive call
is never sent (`completions.calls == 0`), and an affordable call still goes
through — fail-closed, not shut.
