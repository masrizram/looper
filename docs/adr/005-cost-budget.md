# ADR-005: Hard cost budget per build

## Status
Accepted (2026-08-08)

## Context
Autonomous agents loop, calling paid LLM APIs each iteration. Without a spend
ceiling, a stuck loop (bug A -> fix B -> bug A) can burn the API budget in
minutes producing no valid code. The previous design only *tracked* token usage
for reporting; it never stopped a run.

## Decision
Add `execution.max_cost_usd` (0 = unlimited) and estimate spend as
`total_tokens / 1000 * price_per_1k`. `OpenRouterClient` carries per-model
prices (`model_prices_usd_per_1k`) with a `default_token_price_usd` fallback.
Before each cycle the orchestrator compares `client.running_cost_usd()` to the
ceiling and raises `CostBudgetExceeded` if exceeded. `looper.cli` maps that to
exit code **4**.

## Consequences
- A runaway build aborts fast instead of running up the bill.
- Estimates are token-count based, not exact billing — documented as such.
- `0` preserves the old observe-only behaviour.

## Verification
`tests/test_cli.py::test_cost_budget_exceeded_returns_exit_4` proves the
`CostBudgetExceeded` path yields exit 4.
