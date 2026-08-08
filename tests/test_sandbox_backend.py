"""Proof tests for the cross-platform sandbox backend resolution (ADR-008).

The hole these close: before this, ``run_sandboxed`` degraded to a bare
``subprocess.run`` on any host without ``fork`` -- i.e. every Windows machine
-- while the docs promised OS resource limits. That was the only fail-*open*
path in an otherwise fail-closed system.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from looper.sandbox import (
    SANDBOX_BACKENDS,
    SandboxUnavailableError,
    docker_argv,
    docker_available,
    posix_rlimits_available,
    resolve_backend,
    run_sandboxed,
)


def _runner(returncode: int = 0, *, raises: Exception | None = None):
    """A fake subprocess.run that records the argv it was handed."""
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout="", stderr="", args=argv)

    run.calls = calls  # type: ignore[attr-defined]
    return run


# -- docker detection ----------------------------------------------------


def test_docker_available_true_when_daemon_answers():
    run = _runner(0)
    assert docker_available(run) is True
    # Must query the *server* version: `docker --version` succeeds with a dead
    # daemon and would let us claim isolation we cannot provide.
    assert run.calls[0][:2] == ["docker", "version"]


def test_docker_available_false_on_nonzero_exit():
    assert docker_available(_runner(1)) is False


def test_docker_available_false_when_binary_missing():
    assert docker_available(_runner(raises=FileNotFoundError("no docker"))) is False


def test_docker_available_false_on_timeout():
    exc = subprocess.TimeoutExpired(cmd="docker", timeout=15)
    assert docker_available(_runner(raises=exc)) is False


def test_docker_available_uses_real_subprocess_by_default():
    # Exercises the `runner or subprocess.run` default arm; either answer is
    # legitimate depending on the host, we only require it not to raise.
    assert docker_available() in (True, False)


# -- backend resolution --------------------------------------------------


def test_resolve_rejects_unknown_backend():
    with pytest.raises(ValueError) as err:
        resolve_backend("chroot")
    assert "chroot" in str(err.value)


def test_resolve_none_is_honoured_but_warns(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_backend("none") == "none"
    assert "unconfined" in caplog.text


def test_resolve_auto_prefers_docker():
    assert resolve_backend("auto", runner=_runner(0)) == "docker"


def test_resolve_docker_explicit_succeeds():
    assert resolve_backend("docker", runner=_runner(0)) == "docker"


def test_resolve_docker_explicit_fails_closed_without_daemon():
    with pytest.raises(SandboxUnavailableError) as err:
        resolve_backend("docker", runner=_runner(1))
    assert "no Docker daemon responded" in str(err.value)


def test_resolve_docker_degrades_when_not_fail_closed(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_backend("docker", fail_closed=False, runner=_runner(1)) == "none"
    assert "WITHOUT isolation" in caplog.text


def test_resolve_rlimit_matches_platform_capability():
    if posix_rlimits_available():
        assert resolve_backend("rlimit") == "rlimit"
    else:
        with pytest.raises(SandboxUnavailableError) as err:
            resolve_backend("rlimit")
        assert "no fork/rlimits" in str(err.value)


def test_resolve_rlimit_degrades_when_not_fail_closed():
    expected = "rlimit" if posix_rlimits_available() else "none"
    assert resolve_backend("rlimit", fail_closed=False) == expected


def test_resolve_auto_without_docker_matches_platform():
    no_docker = _runner(1)
    if posix_rlimits_available():
        assert resolve_backend("auto", runner=no_docker) == "rlimit"
    else:
        with pytest.raises(SandboxUnavailableError):
            resolve_backend("auto", runner=no_docker)


def test_resolve_auto_degrades_when_not_fail_closed():
    expected = "rlimit" if posix_rlimits_available() else "none"
    assert resolve_backend("auto", fail_closed=False, runner=_runner(1)) == expected


# Both arms of the rlimit branches must be proven on every OS, otherwise the
# Windows-only failure this ADR fixes would itself be untested on Linux CI
# (and vice versa).


@pytest.fixture
def fake_rlimits(monkeypatch):
    def _set(available: bool):
        monkeypatch.setattr("looper.sandbox.posix_rlimits_available", lambda: available)

    return _set


def test_resolve_rlimit_selected_when_platform_supports_it(fake_rlimits):
    fake_rlimits(True)
    assert resolve_backend("rlimit", runner=_runner(1)) == "rlimit"


def test_resolve_rlimit_refused_when_platform_lacks_fork(fake_rlimits):
    fake_rlimits(False)
    with pytest.raises(SandboxUnavailableError) as err:
        resolve_backend("rlimit", runner=_runner(1))
    assert "no fork/rlimits" in str(err.value)


def test_resolve_auto_falls_back_to_rlimit_without_docker(fake_rlimits):
    fake_rlimits(True)
    assert resolve_backend("auto", runner=_runner(1)) == "rlimit"


def test_resolve_auto_refuses_when_neither_backend_exists(fake_rlimits):
    fake_rlimits(False)
    with pytest.raises(SandboxUnavailableError) as err:
        resolve_backend("auto", runner=_runner(1))
    assert "no sandbox backend available" in str(err.value)


def test_every_declared_backend_is_handled():
    """No declared backend may fall through unresolved."""
    for backend in SANDBOX_BACKENDS:
        try:
            result = resolve_backend(backend, fail_closed=False, runner=_runner(0))
        except SandboxUnavailableError:  # pragma: no cover - fail_closed is off
            pytest.fail(f"{backend} raised despite fail_closed=False")
        assert result in ("docker", "rlimit", "none")


# -- container argv ------------------------------------------------------


def test_docker_argv_is_locked_down():
    argv = docker_argv(
        ["/usr/bin/python", "-m", "pytest", "tests"],
        cwd="/work/space",
        image="python:3.11-slim",
        network="none",
        cpu_seconds=120,
        rss_bytes=512_000_000,
    )
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--pids-limit=256" in argv
    assert "-v" in argv and "/work/space:/work" in argv
    # The host interpreter path is meaningless inside the image.
    assert "/usr/bin/python" not in argv
    assert argv[argv.index("python:3.11-slim") + 1] == "python"
    assert argv[-3:] == ["-m", "pytest", "tests"]


def test_docker_argv_enforces_memory_floor():
    argv = docker_argv(
        ["python", "-c", "pass"],
        cwd="/w",
        image="img",
        network="none",
        cpu_seconds=1,
        rss_bytes=1,
    )
    assert "--memory=64000000b" in argv
    # cpu_seconds below a minute must still request at least one CPU.
    assert "--cpus=1" in argv


# -- run_sandboxed -------------------------------------------------------


def test_run_sandboxed_uses_docker_when_available():
    run = _runner(0)
    run_sandboxed(
        ["python", "-m", "pytest"],
        cwd="/w",
        timeout=30,
        cpu_seconds=60,
        wall_seconds=60,
        rss_bytes=64_000_000,
        backend="docker",
        runner=run,
    )
    # First call probes the daemon, second is the containerised run.
    assert run.calls[1][0] == "docker"
    assert "run" in run.calls[1]


def test_run_sandboxed_refuses_when_no_isolation():
    with pytest.raises(SandboxUnavailableError):
        run_sandboxed(
            ["python", "-m", "pytest"],
            cwd="/w",
            timeout=30,
            cpu_seconds=60,
            wall_seconds=60,
            rss_bytes=64_000_000,
            backend="docker",
            fail_closed=True,
            runner=_runner(1),
        )


def test_run_sandboxed_backend_none_runs_plain_argv():
    run = _runner(0)
    run_sandboxed(
        ["python", "-c", "pass"],
        cwd="/w",
        timeout=5,
        cpu_seconds=1,
        wall_seconds=1,
        rss_bytes=64_000_000,
        backend="none",
        runner=run,
    )
    assert run.calls[-1] == ["python", "-c", "pass"]


def test_run_sandboxed_really_executes_a_process(tmp_path):
    """End-to-end through the real subprocess layer, no fakes."""
    proc = run_sandboxed(
        ["python", "-c", "print('hello from sandbox')"],
        cwd=str(tmp_path),
        timeout=60,
        cpu_seconds=30,
        wall_seconds=30,
        rss_bytes=512_000_000,
        backend="none",
    )
    assert proc.returncode == 0
    assert "hello from sandbox" in proc.stdout
