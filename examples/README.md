# Examples

Five copy-paste scenarios, ordered by cost. Each folder holds a complete
`config.yaml` and — where the scenario needs one — a `user_tests/` suite that
the AI never sees.

Run any of them with:

```bash
looper --config examples/<folder>/config.yaml --goal "<the goal in the README>"
```

Free checks first, always. They cost nothing and catch the two mistakes that
waste real money:

```bash
looper --config examples/<folder>/config.yaml --check-config
looper --config examples/<folder>/config.yaml --check-models   # dead slugs fail MID-build
looper --doctor                                                # can this host isolate anything?
```

| Folder | What it demonstrates | Approx. cost |
|---|---|---|
| [`01-minimal-trial`](01-minimal-trial/) | Smallest possible run: 1 cycle, 3 phases, hard $0.50 ceiling | ~$0.10–0.50 |
| [`02-budget-guard`](02-budget-guard/) | The budget as a real ceiling — deliberately set too low to prove exit `4` | < $1.00 |
| [`03-user-owned-tests`](03-user-owned-tests/) | The anti-overfit gate: a human suite the builder never sees | ~$2–5 |
| [`04-ci-gate`](04-ci-gate/) | Non-interactive release gate driven purely by exit codes | ~$2–5 |
| [`05-daemon-with-webhook`](05-daemon-with-webhook/) | 24/7 mode with outbound Slack/Discord notifications | varies |

## Exit codes you will actually see

| Code | Meaning | Usual cause |
|---|---|---|
| `0` | Score cleared `min_acceptable` | success |
| `2` | Config error | typo in YAML, unknown key |
| `3` | Built, but scored below `min_acceptable` | the gate did its job |
| `4` | Cost ceiling hit | `max_cost_usd` reached |
| `5` | No sandbox available | no Docker/Podman/WSL/rlimits |
| `6` | Provider returned 402 | account out of credits |

## The three mistakes new users make

1. **Skipping `--check-models`.** A wrong slug is a well-formed string, so
   `--check-config` passes it. The build then dies mid-flight, after earlier
   phases have already been billed.
2. **Expecting a build on a host with no isolation.** `--doctor` exits `5` and
   builds refuse to run generated tests. That is the design (ADR-008), not a
   bug. Install Docker, run `wsl --install`, or set
   `execution.sandbox_fail_closed: false` and accept the risk explicitly.
3. **Leaving `user_tests_dir` empty and trusting the score.** Without it the AI
   grades its own homework. Scenario 03 is the one that matters.
