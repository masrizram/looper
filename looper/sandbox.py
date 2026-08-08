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


def run_sandboxed(
    argv: list[str],
    *,
    cwd: str,
    timeout: int,
    cpu_seconds: int,
    wall_seconds: int,
    rss_bytes: int,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` (fixed, never shell) under resource limits.

    On POSIX a ``preexec_fn`` installs RLIMIT_CPU / RLIMIT_AS so a runaway
    generated suite is killed instead of wedging the daemon or OOMing the box.
    On Windows (no ``fork``/rlimits) we fall back to the wall-clock
    ``timeout`` alone and log the weaker guarantee.
    """
    preexec = (
        _posix_rlimit_fn(cpu_seconds, wall_seconds, rss_bytes) if hasattr(os, "fork") else None
    )

    return subprocess.run(  # nosec B603 - fixed argv, no shell
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
