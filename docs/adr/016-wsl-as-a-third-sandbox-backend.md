# ADR-016: WSL as a third sandbox backend

Status: Accepted

## Context

ADR-008 makes the sandbox fail-closed: with no isolation available, Looper
refuses to execute generated tests and exits `5`. That is the right default --
the alternative is running LLM-written code directly on a developer's machine.

The cost was measured on the reference Windows host: `--doctor` reported
`docker: no`, `podman: no`, `POSIX rlimits: no`, and exited `5`. On Windows
without Docker Desktop -- a large fraction of the people who would try this
tool -- **every** build refuses to run. The remedy was a multi-gigabyte install,
which is a steep price for evaluating a CLI.

Those same hosts frequently already have WSL2, or can obtain it with one
command (`wsl --install`).

## Decision

Add `wsl` as a fourth accepted value for `execution.sandbox_backend` and a
third effective backend. Commands run as
`wsl.exe --cd <cwd> -e /bin/sh -lc 'ulimit -t <cpu>; ulimit -v <kb>; ulimit -u 64; exec <cmd>'`,
so CPU, address space and process count are bounded by the same limits the
POSIX `rlimit` backend applies. Windows-absolute paths in the argv are
translated (`C:\ws\tests` -> `/mnt/c/ws/tests`) and the host interpreter path is
replaced with the distro's `python3`, since a `python.exe` path is meaningless
inside the distro.

Availability is probed by **executing** a command (`wsl.exe -e /bin/sh -c 'exit 0'`),
not by listing distros. Measured on the reference host: `wsl -l` exits `0` when
the Windows feature is enabled but no distribution is installed, so a listing
probe reports isolation that does not exist -- a fail-open bug in the one place
that must never fail open.

In `auto`, precedence is **docker > podman > rlimit > wsl**. WSL is last on
purpose: it shares the host network stack and can reach the Windows filesystem
through `/mnt`, so its containment is genuinely weaker than a `--network none`
container. When `wsl` is the effective backend the doctor says so out loud
rather than implying container-grade isolation.

## Consequences

Good: a Windows host with WSL can run builds without Docker Desktop, which
removes the single largest adoption blocker without weakening the default. The
fail-closed contract is unchanged -- a host with none of the four backends still
exits `5`, and its remedy list now names `wsl --install`.

Bad: WSL isolation is weaker than a container, so a user who reads only the
backend name may overestimate it (mitigated by an explicit warning). The probe
costs one process launch, bounded by a 15-second timeout.

## Alternatives rejected

**Silently accept WSL inside `auto` without a warning.** Isolation strength
would then vary by host with nothing in the output saying so.

**Probe with `wsl -l -q`.** Cheaper, and wrong: exits `0` on a host with the
feature enabled and no distro, reporting a sandbox that cannot run anything.

**Loosen `sandbox_fail_closed` on Windows.** Trades a refusal for silent
execution of LLM code on the host -- exactly the failure ADR-008 exists to
prevent.
