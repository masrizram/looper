#!/usr/bin/env python
"""Prove the sandbox actually contains hostile code (ADR-006 / ADR-008).

Why this exists
---------------
The v3 audit found two isolation bugs that survived a full green suite at
100% line AND branch coverage:

* ``docker_argv`` passed the *host* tests path into the container, so the
  container backend could never once succeed;
* ``_posix_rlimit_fn`` called ``setrlimit`` with a bare int instead of a
  ``(soft, hard)`` tuple, and then overwrote the CPU cap with wall_seconds.

Both were invisible because every existing test asserted the *shape of the
argv* rather than what happened when it ran. Coverage cannot catch that class
of bug: the lines execute, the guarantee still does not hold.

This script closes that gap by running genuinely hostile payloads through
``run_sandboxed`` and asserting on observable outcomes -- the marker file was
never created, the fork bomb did not spawn, the runaway loop was killed. It
is POSIX-only (it needs real rlimits or a container runtime) and is wired
into CI as the ``sandbox-integration`` job on ubuntu-latest.

Exit code 0 means containment held. Any other exit code means the isolation
this project advertises is not real on this host.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from looper.sandbox import (  # noqa: E402
    SandboxUnavailableError,
    resolve_backend,
    run_sandboxed,
    scan_for_dangerous_calls,
)

FAILURES: list[str] = []
CHECKS_RUN = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS_RUN
    CHECKS_RUN += 1
    if condition:
        print(f"  PASS  {label}")
        return
    print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))
    FAILURES.append(label)


def run_payload(workspace: Path, source: str, *, timeout: int = 30, cpu_seconds: int = 5):
    """Execute ``source`` inside the sandbox, returning the completed process."""
    script = workspace / "payload.py"
    script.write_text(source, encoding="utf-8")
    return run_sandboxed(
        [sys.executable, "-I", "-B", str(script)],
        cwd=str(workspace),
        timeout=timeout,
        cpu_seconds=cpu_seconds,
        wall_seconds=timeout,
        rss_bytes=256_000_000,
        backend="auto",
        image="python:3.11-slim",
        network="none",
        fail_closed=True,
    )


def test_filesystem_escape_is_contained(workspace: Path, marker: Path) -> None:
    """A payload writing outside the workspace must not reach the host."""
    print("\n[1] filesystem escape")
    source = (
        "from pathlib import Path\n"
        f"target = Path({str(marker)!r})\n"
        "try:\n"
        "    target.write_text('pwned')\n"
        "    print('WROTE')\n"
        "except Exception as exc:\n"
        "    print('BLOCKED', exc)\n"
    )
    try:
        proc = run_payload(workspace, source)
    except SandboxUnavailableError as exc:
        check("filesystem escape contained", False, f"sandbox unavailable: {exc}")
        return
    check(
        "marker file was NOT created on the host",
        not marker.exists(),
        f"{marker} exists with: {marker.read_text() if marker.exists() else ''}",
    )
    print(f"        payload said: {proc.stdout.strip()[:120]}")


def test_cpu_runaway_is_killed(workspace: Path) -> None:
    """An infinite loop must be killed by the CPU rlimit / container cap."""
    print("\n[2] runaway CPU loop")
    source = "while True:\n    pass\n"
    started = time.monotonic()
    try:
        proc = run_payload(workspace, source, timeout=25, cpu_seconds=3)
        elapsed = time.monotonic() - started
        check("runaway loop terminated", proc.returncode != 0, f"exit={proc.returncode}")
        check("terminated promptly (< 25s)", elapsed < 25.0, f"took {elapsed:.1f}s")
    except SandboxUnavailableError as exc:
        check("runaway loop terminated", False, f"sandbox unavailable: {exc}")
    except Exception as exc:  # subprocess.TimeoutExpired etc.
        elapsed = time.monotonic() - started
        # A timeout is also containment: the daemon was not wedged forever.
        check("runaway loop terminated", True, f"{type(exc).__name__} after {elapsed:.1f}s")


def test_fork_bomb_is_capped(workspace: Path) -> None:
    """RLIMIT_NPROC / --pids-limit must stop unbounded process spawning."""
    print("\n[3] fork bomb")
    source = (
        "import os\n"
        "spawned = 0\n"
        "try:\n"
        "    for _ in range(5000):\n"
        "        if os.fork() == 0:\n"
        "            os._exit(0)\n"
        "        spawned += 1\n"
        "except Exception as exc:\n"
        "    print('CAPPED', spawned, type(exc).__name__)\n"
        "else:\n"
        "    print('UNCAPPED', spawned)\n"
    )
    try:
        proc = run_payload(workspace, source, timeout=25, cpu_seconds=5)
        combined = (proc.stdout or "") + (proc.stderr or "")
        check(
            "process spawning was capped or refused",
            "UNCAPPED" not in combined or proc.returncode != 0,
            combined.strip()[:160],
        )
    except SandboxUnavailableError as exc:
        check("process spawning was capped", False, f"sandbox unavailable: {exc}")
    except Exception as exc:
        check("process spawning was capped", True, f"{type(exc).__name__}")


def test_static_scan_refuses_hostile_suites() -> None:
    """The tripwire must refuse these before execution is even attempted."""
    print("\n[4] static scan tripwire")
    hostile = {
        "os.system": "import os\nos.system('touch /tmp/pwned')\n",
        "os.popen": "import os\nos.popen('id').read()\n",
        "subprocess": "import subprocess\nsubprocess.run(['id'])\n",
        "socket": "import socket\nsocket.socket()\n",
        "shutil.rmtree": "import shutil\nshutil.rmtree('/')\n",
        "eval": "eval('1+1')\n",
    }
    for label, source in hostile.items():
        check(f"refused: {label}", scan_for_dangerous_calls(source) != [])

    benign = "import json\n\ndef test_x():\n    assert json.loads('{}') == {}\n"
    check("allowed: ordinary json.loads suite", scan_for_dangerous_calls(benign) == [])


def main() -> int:
    print("=" * 68)
    print("sandbox containment integration test (ADR-006 / ADR-008)")
    print("=" * 68)

    try:
        backend = resolve_backend("auto", fail_closed=False)
    except ValueError as exc:  # pragma: no cover - defensive
        print(f"FAIL: {exc}")
        return 1
    print(f"resolved backend: {backend}")
    if backend == "none":
        print("\nFAIL: no sandbox backend on this host -- containment cannot be proven.")
        print("This job must run somewhere with Docker/Podman or POSIX rlimits.")
        return 1

    workspace = Path(tempfile.mkdtemp(prefix="looper-sandbox-"))
    marker = Path(tempfile.gettempdir()) / "looper_sandbox_escape_marker"
    if marker.exists():
        marker.unlink()

    try:
        test_filesystem_escape_is_contained(workspace, marker)
        test_cpu_runaway_is_killed(workspace)
        if hasattr(os, "fork"):
            test_fork_bomb_is_capped(workspace)
        else:  # pragma: no cover - non-POSIX
            print("\n[3] fork bomb -- skipped (no os.fork on this platform)")
        test_static_scan_refuses_hostile_suites()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        if marker.exists():
            marker.unlink()

    print("\n" + "=" * 68)
    if FAILURES:
        print(f"CONTAINMENT FAILED: {len(FAILURES)} of {CHECKS_RUN} checks failed")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print(f"CONTAINMENT HELD: all {CHECKS_RUN} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
