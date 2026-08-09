# 04 - CI release gate

**Goal to use:** whatever the pipeline is building, passed as `--goal`.

Looper is a CLI with deterministic exit codes, so it drops into CI as a step.

```yaml
# .github/workflows/ai-gate.yml (excerpt)
- name: Free pre-flight checks
  run: |
    looper --config examples/04-ci-gate/config.yaml --check-config
    looper --config examples/04-ci-gate/config.yaml --check-models
    looper --doctor

- name: AI build behind the gate
  env:
    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
  run: looper --config examples/04-ci-gate/config.yaml --json-logs \
              --goal "${{ inputs.goal }}"
```

## Why the pre-flight checks are separate steps

They cost nothing and they fail for different reasons than the build does:

* `--check-config` catches YAML mistakes (exit `2`).
* `--check-models` catches a dead model slug. A wrong slug is a valid string,
  so it passes `--check-config` and then kills the build **mid-flight, after
  earlier phases are already billed**.
* `--doctor` catches a runner with no container runtime (exit `5`) before you
  pay for anything.

## Reading the outcome

| Exit | CI meaning |
|---|---|
| `0` | merge candidate |
| `3` | the gate rejected it -- read the score breakdown in the state file |
| `4` | raise `max_cost_usd` or narrow the goal |
| `5` | the runner cannot isolate; fix the runner, do not disable the sandbox |
| `6` | top up the provider account |

Use `--json-logs` so the log shipper gets structured lines.

## Resuming a run the runner killed

If a job is cancelled mid-build, re-run with the **same goal** and `--resume`
to skip the research and architecture phases that were already paid for:

```bash
looper --config examples/04-ci-gate/config.yaml --resume --goal "<same goal>"
```

Only unscored input phases are skipped. Build, test, review and the security
audit always re-run, because a cycle may only score evidence it re-verified.
