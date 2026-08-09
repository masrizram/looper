# 02 - The budget guard

**Goal to use:** `build a JSON schema validator`

This scenario is designed to **fail**, and to fail in a specific way.

```bash
looper --config examples/02-budget-guard/config.yaml \
       --goal "build a JSON schema validator"
echo $?      # 4 == EXIT_COST_EXCEEDED
```

## What you should see

```
Cost budget 0.05 USD exhausted ...; aborting.
```

and `looper_state_budget.json` holding `"status": "cost_exhausted"`.

## Why it matters

`max_cost_usd` reserves the projected worst-case cost of a call **before**
sending it (ADR-013). Checking the spent total *after* each call -- the
obvious implementation -- makes the ceiling unenforceable: the first call
always goes through at whatever it costs. On an Opus roster that turned a
$1.00 budget into a $15.00 charge.

## Turning it into a real config

Raise `max_cost_usd` to your actual per-build limit. Keep it non-zero: `0`
disables the cap entirely, which is the right setting only when something else
is bounding your spend.
