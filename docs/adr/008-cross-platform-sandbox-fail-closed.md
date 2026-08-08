# ADR-008: Cross-platform sandbox, fail-closed

Status: accepted
Date: 2026-08-08

## Context

`run_sandboxed` installed POSIX rlimits through a `preexec_fn` guarded by
`hasattr(os, "fork")`. On Windows that guard is always false, so the function
silently degraded to a bare `subprocess.run` with only a wall-clock timeout —
while README and ADR-006 promised CPU, memory and process isolation for
LLM-authored test suites.

This was the single **fail-open** path in a system whose entire design thesis
is fail-closed: scoring caps unverified builds, the cost budget aborts hard,
the static scanner refuses dangerous suites. Isolation quietly disappearing on
a whole platform contradicted all of it, and it was invisible: nothing in the
logs or the CLI told the operator their sandbox did not exist.

Docker is the only isolation primitive that behaves identically on Linux,
macOS and Windows.

## Decision

1. Introduce `execution.sandbox_backend`: `auto | rlimit | docker | none`.
   `auto` prefers Docker, then POSIX rlimits.
2. Docker detection probes `docker version --format {{.Server.Version}}`, not
   `docker --version`: the latter succeeds with a dead daemon and would let us
   claim isolation we cannot provide.
3. The Docker backend runs a throwaway container: `--rm`, `--network=none`,
   `--read-only`, `--pids-limit`, cpu/memory caps, `--security-opt=no-new-privileges`,
   and only the workspace bind-mounted.
4. Add `execution.sandbox_fail_closed` (default **true**). When no backend is
   available, `SandboxUnavailableError` is raised and the test phase returns
   `0 passed, 1 failed`. The build loses the test weight and is capped at 60 by
   the existing `unverified_build` gate — an unsandboxable host cannot ship.
5. Add `looper --doctor`, which reports the isolation actually available and
   exits `5` when the configured policy cannot be met, so CI can gate on it.

## Consequences

* On a Windows host with no Docker, builds now **refuse** to execute generated
  tests instead of running them unconfined. That is a deliberate, breaking
  behaviour change; `sandbox_fail_closed: false` restores the old behaviour
  with a loud warning.
* Tests that legitimately run pytest on the host (the user-suite plumbing
  tests) opt out explicitly with `sandbox_tests: false`.
* Exit code `5` joins the CLI contract.
