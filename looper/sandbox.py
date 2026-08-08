"""Static guardrails for LLM-authored test/code before it runs.

The pipeline executes code written by a language model (``POST /build`` is
RCE by design). A fixed-argv ``subprocess.run`` without ``shell=True`` stops
shell injection, but it does NOT stop the generated Python itself from doing
something destructive (``os.remove``), forking, calling the network, or
looping forever. These helpers refuse to run a suite that contains such
patterns, and launch the remaining suite under OS resource limits so a stray
``while True`` or memory hog cannot wedge or OOM the host.

See ADR-005 (verified evidence, no phantom findings) and ADR-006 (sandbox).
"""

from __future__ import annotations

import ast
import logging
import os
import subprocess  # nosec B404 - used with a fixed argv, never shell=True
from typing import Callable

logger = logging.getLogger("looper.sandbox")

#: Substring fragments whose *presence* (outside comments) means "do not run
#: this untrusted blob on the host". Call-style fragments (subprocess.run,
#: socket.socket) require the call; a bare ``import subprocess`` is NOT enough,
#: and "# a comment about os.system" is stripped before scanning.
DESTRUCTIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("os.system", "shell execution via os.system"),
    ("os.remove", "filesystem deletion via os.remove"),
    ("os.unlink", "filesystem deletion via os.unlink"),
    ("os.rmdir", "filesystem deletion via os.rmdir"),
    ("shutil.rmtree", "recursive filesystem deletion via shutil.rmtree"),
    ("os.rename", "filesystem move via os.rename"),
    ("subprocess.run", "child process spawn via subprocess.run"),
    ("subprocess.Popen", "child process spawn via subprocess.Popen"),
    ("subprocess.call", "child process spawn via subprocess.call"),
    ("socket.", "raw socket / network access"),
    ("requests.", "outbound HTTP via requests"),
    ("urllib.request", "outbound HTTP via urllib"),
    ("httpx.", "outbound HTTP via httpx"),
    ("__import__", "dynamic import via __import__"),
    ("eval(", "dynamic execution via eval"),
    ("exec(", "dynamic execution via exec"),
    ("marshal.loads", "code loading via marshal"),
    ("pickle.loads", "deserialization via pickle"),
    ("os.fork", "process forking"),
    ("os.kill", "signal delivery via os.kill"),
    ("ctypes", "native code via ctypes"),
)


def _strip_comments(source: str) -> str:
    """Drop ``#``-to-end-of-line comments so prose mentions aren't flagged.

    Strings are left intact; a ``#`` inside a string is rare in guardrail code
    and keeping it only makes the scan stricter, which is safe here.
    """
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


def scan_for_dangerous_calls(source: str) -> list[str]:
    """Return human-readable reasons the source looks unsafe to execute.

    An empty list means "looks ok". Comments are stripped first; bare imports
    of a dangerous module (e.g. ``import subprocess``) are not enough on their
    own because the ambiguous fragments require the call form (``.run``, etc.).
    An AST pass adds detection of dangerous *calls* that the substring scan
    could miss (e.g. ``getattr(os, "system")``).
    """
    reasons: list[str] = []
    scanned = _strip_comments(source)
    for fragment, reason in DESTRUCTIVE_PATTERNS:
        if fragment in scanned:
            reasons.append(reason)
    # AST pass: only flag dangerous names that are actually *called*.
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return reasons
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    dangerous_calls = {
        "system",
        "remove",
        "unlink",
        "rmdir",
        "rename",
        "fork",
        "kill",
        "eval",
        "exec",
        "loads",
    }
    for name in called & dangerous_calls:
        reasons.append(f"calls dangerous builtin/function '{name}'")
    return reasons


class SandboxUnavailableError(RuntimeError):
    """No isolation backend is available and the policy is fail-closed.

    Raised instead of silently executing LLM-authored code unconfined. The
    previous behaviour degraded to a bare ``subprocess.run`` on any platform
    without ``fork`` (i.e. every Windows host) while the documentation still
    promised resource limits -- the one fail-*open* path in an otherwise
    fail-closed system. See ADR-008.
    """


#: Accepted values for ``execution.sandbox_backend``.
SANDBOX_BACKENDS: tuple[str, ...] = ("auto", "rlimit", "docker", "podman", "none")

#: Effective backends ``resolve_backend`` may return.
EFFECTIVE_BACKENDS: tuple[str, ...] = ("rlimit", "docker", "podman", "none")

#: Container runtimes share one locked-down ``run`` contract (read-only,
#: no network, cpu/memory/pids capped). Either binary satisfies it.
CONTAINER_RUNTIMES: tuple[str, ...] = ("docker", "podman")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def posix_rlimits_available() -> bool:
    """True when ``preexec_fn`` + ``resource`` rlimits can be installed."""
    return hasattr(os, "fork")


def docker_available(runner: Runner | None = None) -> bool:
    """True when a responsive Docker daemon can be reached.

    ``docker version`` is used rather than ``docker --version`` because the
    latter succeeds even when the daemon is down, which would let us claim
    container isolation we cannot actually provide.
    """
    return _docker_probe("docker", runner)


def podman_available(runner: Runner | None = None) -> bool:
    """True when a running Podman machine (and the ``podman`` binary) exists.

    Podman is a drop-in for Docker for the ``run`` call, but its ``version``
    command exits 0 even when **no machine is running** -- the same fail-open
    trap ADR-008 closed for ``docker --version``. So we probe ``podman info``
    (which reaches the machine/VM) and treat anything that does not confirm a
    live runtime as "not available" rather than "isolation ready".
    """
    return _docker_probe("podman", runner, info=True)


def _docker_probe(binary: str, runner: Runner | None, *, info: bool = False) -> bool:
    """Probe a Docker-compatible runtime for a *responsive* daemon/machine.

    For Docker the canonical probe is ``<bin> version`` (server component).
    For Podman we use ``<bin> info``, because ``podman version`` reports the
    client version and exits 0 even with no machine booted. Either probe must
    return a clean exit code, or we report no isolation.
    """
    run = runner or subprocess.run
    argv = [binary, "info"] if info else [binary, "version", "--format", "{{.Server.Version}}"]
    try:
        proc = run(  # nosec B603 B607 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def container_runtime_available(*, runner: Runner | None = None) -> str | None:
    """First available Docker-compatible runtime, or ``None``.

    Prefers Docker over Podman: the existing agents' ``sandbox_image`` default
    and ``docker_argv`` wiring are Docker-shaped, and Podman's rootless setup
    is a strict superset of what Docker needs. Returning the binary name lets
    ``resolve_backend`` feed it straight into the shared ``docker_argv``.
    """
    for runtime in CONTAINER_RUNTIMES:
        if runtime == "docker" and docker_available(runner):
            return "docker"
        if runtime == "podman" and podman_available(runner):
            return "podman"
    return None


def resolve_backend(
    requested: str,
    *,
    fail_closed: bool = True,
    runner: Runner | None = None,
) -> str:
    """Pick the effective isolation backend, or refuse.

    ``auto`` prefers Docker (equivalent isolation on every OS), then POSIX
    rlimits. When nothing is available the decision is the caller's policy:
    ``fail_closed=True`` raises :class:`SandboxUnavailableError` so untrusted
    code is never run unconfined, ``False`` degrades to ``none`` with a loud
    warning.
    """
    if requested not in SANDBOX_BACKENDS:
        raise ValueError(f"unknown sandbox backend {requested!r}; expected {SANDBOX_BACKENDS}")

    if requested == "none":
        logger.warning(
            "sandbox_backend='none': LLM-authored tests will run unconfined on this host"
        )
        return "none"

    if requested in ("docker", "podman"):
        if requested == "docker" and docker_available(runner):
            return "docker"
        if requested == "podman" and podman_available(runner):
            return "podman"
        message = (
            "docker backend requested but no Docker daemon responded"
            if requested == "docker"
            else "podman backend requested but no running Podman machine was found"
        )
        return _unavailable(message, fail_closed)

    if requested == "rlimit":
        if posix_rlimits_available():
            return "rlimit"
        return _unavailable(
            "rlimit backend requested but this platform has no fork/rlimits", fail_closed
        )

    # auto
    runtime = container_runtime_available(runner=runner)
    if runtime is not None:
        return runtime
    if posix_rlimits_available():
        return "rlimit"
    return _unavailable(
        "no sandbox backend available (no Docker/Podman daemon, no POSIX rlimits)", fail_closed
    )


def _unavailable(message: str, fail_closed: bool) -> str:
    if fail_closed:
        raise SandboxUnavailableError(message)
    logger.warning("%s; running WITHOUT isolation because sandbox_fail_closed is false", message)
    return "none"


def docker_argv(
    argv: list[str],
    *,
    cwd: str,
    image: str,
    network: str,
    cpu_seconds: int,
    rss_bytes: int,
    runtime: str = "docker",
) -> list[str]:
    """Wrap ``argv`` so it runs inside a throwaway, network-isolated container.

    The host interpreter path in ``argv[0]`` is meaningless inside the image,
    so it is replaced with the container's ``python``. The workspace is the
    only thing mounted, which also contains the filesystem blast radius.
    ``runtime`` is the resolved binary (``docker`` or ``podman``); both share
    this exact ``run`` contract (read-only, no network, capped cpu/mem/pids).
    """
    return [
        runtime,
        "run",
        "--rm",
        f"--network={network}",
        f"--cpus={max(1, cpu_seconds // 60) if cpu_seconds >= 60 else 1}",
        f"--memory={max(64_000_000, rss_bytes)}b",
        "--pids-limit=256",
        "--read-only",
        "--tmpfs=/tmp:rw,size=64m",  # nosec B108 - container-internal tmpfs, not a host path
        "--security-opt=no-new-privileges",
        "-v",
        f"{cwd}:/work",
        "-w",
        "/work",
        image,
        "python",
        *argv[1:],
    ]


def run_sandboxed(
    argv: list[str],
    *,
    cwd: str,
    timeout: int,
    cpu_seconds: int,
    wall_seconds: int,
    rss_bytes: int,
    backend: str = "auto",
    image: str = "python:3.11-slim",
    network: str = "none",
    fail_closed: bool = True,
    runner: Runner | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` (fixed, never shell) under the strongest isolation available.

    * ``docker`` -- throwaway read-only container, no network, cpu/memory/pids
      capped. Identical guarantees on Linux, macOS and Windows (needs a Docker
      daemon / Desktop).
    * ``podman`` -- same throwaway container contract via the ``podman`` binary
      (needs a running Podman machine on Windows/macOS). The ``run`` flags are
      shared with Docker; only the binary differs.
    * ``rlimit`` -- POSIX ``preexec_fn`` installing RLIMIT_CPU / RLIMIT_AS.
    * ``none``   -- no isolation; only reachable when the caller explicitly
      opted out or set ``fail_closed=False``.

    Raises :class:`SandboxUnavailableError` when isolation was required but
    could not be provided.
    """
    run = runner or subprocess.run
    effective = resolve_backend(backend, fail_closed=fail_closed, runner=runner)

    if effective in ("docker", "podman"):
        return run(  # nosec B603 - fixed argv, no shell
            docker_argv(
                argv,
                cwd=cwd,
                image=image,
                network=network,
                cpu_seconds=cpu_seconds,
                rss_bytes=rss_bytes,
                runtime=effective,
            ),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    preexec = (
        _posix_rlimit_fn(cpu_seconds, wall_seconds, rss_bytes) if effective == "rlimit" else None
    )
    return run(  # nosec B603 - fixed argv, no shell
        argv,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        check=False,
        preexec_fn=preexec,
    )


def _posix_rlimit_fn(  # pragma: no cover - POSIX-only
    cpu_seconds: int, wall_seconds: int, rss_bytes: int
) -> "Callable[[], None]":
    """Build a ``preexec_fn`` that installs CPU/AS rlimits (POSIX assumes fork)."""
    import resource

    def _preexec() -> None:
        def _set(limit: int, value: int) -> None:
            try:
                resource.setrlimit(limit, (value, value))  # type: ignore[attr-defined]
            except (ValueError, OSError) as exc:
                logger.warning("Could not set rlimit %s: %s", limit, exc)

        resource.setrlimit(  # type: ignore[attr-defined]
            resource.RLIMIT_CPU, max(1, cpu_seconds)  # type: ignore[attr-defined]
        )
        resource.setrlimit(  # type: ignore[attr-defined]
            resource.RLIMIT_AS, max(1, rss_bytes)  # type: ignore[attr-defined]
        )
        # RLIMIT_CPU only fires on wall-clock-ish CPU time; gate wall time too.
        resource.setrlimit(  # type: ignore[attr-defined]
            resource.RLIMIT_CPU, max(1, wall_seconds)  # type: ignore[attr-defined]
        )

    return _preexec
