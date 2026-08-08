# ADR-006: Sandbox untrusted generated code + anti-overfitting gate

## Status
Accepted (2026-08-08)

## Context
`POST /build` runs LLM-written code. A fixed-argv `subprocess.run` without
`shell=True` stops shell injection, but the generated *Python* itself can still
`os.system("rm -rf /")`, fork, hit the network, or loop forever. Separately,
because the AI authors both the code and its tests, it can write a trivially
passing suite (`assert True`) and green-light a hollow build.

## Decision
- **Static refusal (`looper.sandbox.scan_for_dangerous_calls`):** before
  running a generated suite, scan it for `os.system`, `subprocess`, `socket`,
  `requests`, `eval`/`exec`, `__import__`, `os.remove`, `shutil.rmtree`, etc.
  Refuse to execute if any appear.
- **Resource limits (`run_sandboxed`):** on POSIX, install `RLIMIT_CPU` /
  `RLIMIT_AS` via a `preexec_fn` so a runaway suite is killed, not wedge-OOM'd.
  Windows falls back to the wall-clock timeout.
- **Adequacy gate (`looper.adequacy`):** reject suites below
  `min_test_assertions_per_100_lines` and any suite that hardcodes the expected
  score/verdict.
- **User tests (`user_tests_dir`):** if set, the generated code must ALSO pass a
  suite the AI never sees, giving a real correctness signal.
- **Lint gate (`lint_generated`):** `py_compile`/`flake8` the generated code
  before accepting it.

## Consequences
- Dangerous generated code is never executed on the host.
- A weak self-test cannot carry a build to green.
- Added execution cost (one extra static scan + optional lint) is negligible
  vs. the cost of a destructive run.

## Verification
`tests/test_sandbox.py` (scan + adequacy) and
`tests/test_phases.py` (`test_generated_suite_with_destructive_call_is_refused`,
`test_generated_suite_with_trivial_assert_is_refused`,
`test_lint_gate_fails_generated_code_with_syntax_error`,
`test_user_tests_run_when_configured`) prove each layer fires.
