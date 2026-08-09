# Safeguards

Looper executes code written by a language model, so it is hardened against the
failure modes of autonomous agents by design. Each safeguard below exists
because the unguarded behaviour was a real hole, not a hypothetical one.

- [The safeguards](#the-safeguards)
- [Configuration reference](#configuration-reference)

> `POST /build` runs LLM-authored code. Treat it as remote code execution by
> design — see [SECURITY.md](../SECURITY.md).

---

## The safeguards

**Cost ceiling (`max_cost_usd`).** Each build tracks estimated API spend (token
usage × per-model price). Before every request the client *reserves* the call's
worst-case cost — estimated prompt tokens plus the agent's `max_tokens`, at that
model's price — and refuses the call outright if the reservation would cross the
ceiling. Nothing is sent, so the refusal is free, and spend never exceeds the
ceiling rather than exceeding it by one call and reporting so afterwards
([ADR-005](adr/005-cost-budget.md),
[ADR-013](adr/013-reserve-budget-before-the-call.md)). Crossing the ceiling
aborts the build *hard* and exits `4` — no silent bill runaway. Prices for the
default roster ship with the package; a model with no price falls back to
`default_token_price_usd`, which under-reports frontier models badly, so keep
the table current. Set `completion_prices_usd_per_1k` if you know your real
input/output split and want exact rather than blended accounting.

**Untrusted-code sandbox, fail-closed (`sandbox_backend`).** Generated test
suites run either in a throwaway Docker container (read-only, `--network=none`,
cpu/memory/pids capped, no-new-privileges) or under POSIX rlimits. A static
scan *refuses to run* any suite that shells out, spawns processes, touches the
network, or uses `eval`/`exec`. **If no isolation backend is available, the
suite is refused rather than run unconfined** — this previously degraded
silently to no sandbox on Windows (ADR-006,
[ADR-008](adr/008-cross-platform-sandbox-fail-closed.md)). Check your host:

```bash
looper --doctor        # exit 5 means this host cannot isolate anything
```

**Anti-overfitting (`user_tests_dir` + `min_test_assertions_per_100_lines`).**
The AI writes both the code *and* its tests, which is a structural conflict of
interest. A weak suite cannot green-light a build: it must clear an
assertion-density floor, must not hardcode the expected verdict, and — if you
supply `user_tests_dir` — the generated code must also pass *your* tests, which
the AI never sees (ADR-006).

**Lint gate (`lint_generated`).** Generated code is `py_compile`/`flake8`
checked before acceptance, so output that does not even compile never reaches
`done`.

**Scope guard.** Every agent prompt injects a strict "stay within the goal, do
not shell out, do not hallucinate changes to pass tests" directive, so a long
loop cannot drift off the original task
([ADR-007](adr/007-scope-guard.md)).

**Loop cap (`max_cycles`).** A build always stops after N cycles; a human
evaluates anything still failing (ADR-001).

---

## Configuration reference

All safeguards live under the `execution` key in `config.yaml`.

### Cost

| Key | Default | Purpose |
| --- | --- | --- |
| `max_cost_usd` | `0.0` (off) | Hard USD ceiling per build, enforced inside `OpenRouterClient.call` so it fires mid-cycle, not only at the next cycle boundary; `0` disables the abort (ADR-005, ADR-012). |
| `model_prices_usd_per_1k` | verified roster prices | Per-model overrides, merged over the shipped defaults. |
| `default_token_price_usd` | `0.002` | Used when a model has no explicit price. |
| `findings_volume_threshold` | `15` | Findings count at which a report is capped like a critical one, whatever the individual severities (ADR-012). |

### Sandbox

| Key | Default | Purpose |
| --- | --- | --- |
| `sandbox_tests` | `true` | Run generated suites under isolation. |
| `sandbox_backend` | `auto` | `auto` \| `rlimit` \| `docker` \| `none`. `auto` prefers Docker (ADR-008). |
| `sandbox_fail_closed` | `true` | Refuse to run generated tests when no isolation exists. |
| `sandbox_image` | `python:3.11-slim` | Image used by the Docker backend. |
| `sandbox_network` | `none` | Container network mode. |
| `sandbox_cpu_seconds` | `60` | CPU-time cap for one test run: a POSIX `RLIMIT_CPU` under the rlimit backend, and `--ulimit cpu=` under Docker/Podman. (`--cpus` is only a scheduler share and never stops a runaway loop, so it is not the cap — ADR-012.) |
| `sandbox_wall_seconds` | `300` | POSIX wall-time rlimit. |
| `sandbox_rss_bytes` | `1_000_000_000` | POSIX address-space rlimit. |

### Verification integrity

| Key | Default | Purpose |
| --- | --- | --- |
| `min_test_assertions_per_100_lines` | `6` | Assertion-density floor, counting `assert`, `self.assert*` and `pytest.raises`; a suite that imports nothing under test is refused whatever its density. `0` disables the floor (ADR-012). |
| `user_tests_dir` | `""` (off) | Dir of human-owned tests the AI cannot see or edit. |
| `lint_generated` | `"py_compile"` | `off` \| `py_compile` \| `flake8` gate on generated code. |

### Artifacts & review trail

| Key | Default | Purpose |
| --- | --- | --- |
| `artifact_mode` | `single_file` | `single_file` \| `package` — see [artifacts](artifacts.md). |
| `max_files_per_build` | `25` | Cap on files a package build may write. |
| `git.enabled` | `false` | Commit each cycle to a branch for review (ADR-010). |
