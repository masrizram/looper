# 01 - Minimal trial

**Goal to use:** `build a CLI unit converter for length and weight`

The smallest useful run: three phases, one cycle, `$0.50` ceiling.

```bash
looper --config examples/01-minimal-trial/config.yaml --check-config
looper --config examples/01-minimal-trial/config.yaml --check-models
looper --config examples/01-minimal-trial/config.yaml \
       --goal "build a CLI unit converter for length and weight"
```

## What to expect

* Artifact at `workspace/trial/src/generated_code.py`.
* Generated suite at `workspace/trial/tests/test_generated.py`, executed in a
  sandbox. If `--doctor` says no backend is available, this run exits `5`
  before spending anything on the test phase -- that is the fail-closed
  guarantee working.
* `target_score` is 60, not the default 99. A 3-phase pipeline has no review
  and no security audit, so those score components are 0 by construction and
  a 99 target would be unreachable. Do not copy these thresholds into a real
  config.

## What this scenario does NOT prove

The AI wrote the code and the tests. Nothing here stops it grading its own
homework -- that is scenario 03.
