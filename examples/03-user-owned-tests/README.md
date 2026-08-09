# 03 - Human-owned tests (the anti-overfit gate)

**Goal to use:** `build a shopping cart module with add, remove and total`

This is the scenario that makes Looper different from every other AI coding
agent: the exam is not written by the candidate.

```bash
looper --config examples/03-user-owned-tests/config.yaml \
       --goal "build a shopping cart module with add, remove and total"
```

## How it works

`execution.user_tests_dir` points at `user_tests/`. That suite is:

* **never shown to any agent** -- not in the build prompt, not in the fix prompt;
* **never regenerated** -- the fix phase cannot edit its way to a pass;
* **folded into the score** -- failures count even when the AI's own suite is green.

## Write your tests first

`user_tests/test_contract.py` is a template, not a formality. The four tests
worth stealing from it are the ones LLMs habitually get wrong:

* the empty case (`total()` of an empty cart),
* the idempotent case (`remove()` of something never added),
* the rejection case (negative quantity must raise),
* the existence case (the expected public API is actually exported).

## Pitfall: container backends and paths

Under Docker/Podman only the workspace is bind-mounted. A `user_tests_dir`
outside it is **skipped with a warning**, not silently passed. Keep the suite
beside the config as it is here.
