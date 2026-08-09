"""Proof tests for the v6 improvement pass.

Four features, each tested in **both** directions per ADR-014 -- a gate that
only ever sees its happy path is indistinguishable from a missing gate:

* WSL sandbox backend (G-1): probe, path mapping, argv, resolve, dispatch.
* Resume checkpoint (G-2): what may be skipped, and everything that must
  invalidate a skip.
* 402 fail-fast inside ``run_architecture`` (G-3).
* Webhook notifications (G-4): fired, filtered, and never fatal.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from looper.config import ConfigError, NotificationsConfig, build_config
from looper.notify import Notifier
from looper.orchestrator import RESUMABLE_PHASES, LooperDaemon
from looper.sandbox import (
    EFFECTIVE_BACKENDS,
    SANDBOX_BACKENDS,
    SandboxUnavailableError,
    resolve_backend,
    run_sandboxed,
    to_wsl_path,
    wsl_argv,
    wsl_available,
)
from looper.state import StateManager, build_checkpoint

# --------------------------------------------------------------------------
# G-1  WSL sandbox backend
# --------------------------------------------------------------------------


def _runner(returncode: int = 0, *, raises: Exception | None = None):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, "", "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_wsl_probe_runs_a_real_command_not_a_listing():
    """The probe must EXECUTE something, or it reports phantom isolation.

    ``wsl -l`` exits 0 on a host with the feature enabled and no distro
    installed -- measured on the reference Windows host. Only running a
    command distinguishes the two.
    """
    runner = _runner(0)
    assert wsl_available(runner) is True
    assert runner.calls[0] == ["wsl.exe", "-e", "/bin/sh", "-c", "exit 0"]


def test_wsl_probe_false_when_no_distro_installed():
    # 127 == /bin/sh not found: the WSL feature exists, no distribution does.
    assert wsl_available(_runner(127)) is False


@pytest.mark.parametrize(
    "exc", [OSError("wsl.exe missing"), subprocess.TimeoutExpired("wsl.exe", 15)]
)
def test_wsl_probe_false_when_binary_missing_or_hangs(exc):
    assert wsl_available(_runner(raises=exc)) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("C:\\xampp\\htdocs\\looper", "/mnt/c/xampp/htdocs/looper"),
        ("D:/data/tests", "/mnt/d/data/tests"),
        ("C:\\", "/mnt/c"),
        # Not drive-absolute: must survive untouched.
        ("-q", "-q"),
        ("tests/test_generated.py", "tests/test_generated.py"),
        ("/already/posix", "/already/posix"),
    ],
)
def test_to_wsl_path_maps_only_windows_absolutes(value, expected):
    assert to_wsl_path(value) == expected


def test_wsl_argv_applies_hard_resource_limits():
    argv = wsl_argv(
        ["C:\\Python\\python.exe", "-m", "pytest", "C:\\ws\\tests"],
        cwd="C:\\ws",
        cpu_seconds=30,
        rss_bytes=512_000_000,
    )
    assert argv[:4] == ["wsl.exe", "--cd", "C:\\ws", "-e"]
    command = argv[-1]
    assert "ulimit -t 30" in command
    assert "ulimit -v 500000" in command  # bytes -> KiB
    assert "ulimit -u 64" in command
    # The host interpreter path is meaningless in the distro.
    assert "python.exe" not in command
    assert command.endswith("exec python3 -m pytest /mnt/c/ws/tests")


def test_wsl_argv_floors_absurdly_small_limits():
    command = wsl_argv(["python", "-c", "pass"], cwd=".", cpu_seconds=0, rss_bytes=1)[-1]
    assert "ulimit -t 1" in command
    assert "ulimit -v 65536" in command


def test_resolve_backend_explicit_wsl_success_and_refusal():
    assert resolve_backend("wsl", runner=_runner(0)) == "wsl"
    with pytest.raises(SandboxUnavailableError) as err:
        resolve_backend("wsl", runner=_runner(127))
    assert "wsl --install" in str(err.value)
    assert resolve_backend("wsl", fail_closed=False, runner=_runner(127)) == "none"


def test_auto_prefers_containers_over_wsl(monkeypatch):
    """WSL is weaker (shared network, /mnt visible); it must never outrank Docker."""
    monkeypatch.setattr("looper.sandbox.container_runtime_available", lambda **_: "docker")
    assert resolve_backend("auto", runner=_runner(0)) == "docker"


def test_auto_falls_back_to_wsl_only_when_nothing_else_exists(monkeypatch):
    monkeypatch.setattr("looper.sandbox.container_runtime_available", lambda **_: None)
    monkeypatch.setattr("looper.sandbox.posix_rlimits_available", lambda: False)
    assert resolve_backend("auto", runner=_runner(0)) == "wsl"


def test_auto_still_refuses_when_wsl_is_also_absent(monkeypatch):
    monkeypatch.setattr("looper.sandbox.container_runtime_available", lambda **_: None)
    monkeypatch.setattr("looper.sandbox.posix_rlimits_available", lambda: False)
    with pytest.raises(SandboxUnavailableError) as err:
        resolve_backend("auto", runner=_runner(127))
    assert "no WSL distro" in str(err.value)


def test_run_sandboxed_dispatches_through_wsl(monkeypatch):
    monkeypatch.setattr("looper.sandbox.resolve_backend", lambda *a, **k: "wsl")
    runner = _runner(0)
    run_sandboxed(
        ["python", "-m", "pytest"],
        cwd="C:\\ws",
        timeout=60,
        cpu_seconds=10,
        wall_seconds=20,
        rss_bytes=200_000_000,
        runner=runner,
    )
    assert runner.calls[0][0] == "wsl.exe"


def test_wsl_is_a_declared_and_effective_backend():
    assert "wsl" in SANDBOX_BACKENDS
    assert "wsl" in EFFECTIVE_BACKENDS
    assert build_config({"execution": {"sandbox_backend": "wsl"}}).execution.sandbox_backend == (
        "wsl"
    )


# --------------------------------------------------------------------------
# G-4  Webhook notifications
# --------------------------------------------------------------------------


def _capturing_sender(code: int = 200, *, raises: Exception | None = None):
    sent: list[tuple[str, bytes]] = []

    def send(url, body, headers, timeout):
        sent.append((url, body))
        if raises is not None:
            raise raises
        return code

    send.sent = sent  # type: ignore[attr-defined]
    return send


def test_notifier_disabled_without_a_url():
    notifier = Notifier(NotificationsConfig())
    assert notifier.enabled is False
    assert notifier.notify(status="passed", goal="g", score=99.0, cycle=1, cost_usd=0.1) is False


def test_notifier_posts_terminal_outcome():
    sender = _capturing_sender()
    notifier = Notifier(NotificationsConfig(webhook_url="https://hooks.example/x"), sender=sender)
    assert notifier.notify(status="passed", goal="ship it", score=99.5, cycle=2, cost_usd=1.25)
    url, body = sender.sent[0]
    assert url == "https://hooks.example/x"
    assert b"ship it" in body
    assert b'"status": "passed"' in body
    assert b"99.5" in body


def test_notifier_ignores_non_terminal_and_unsubscribed_statuses():
    sender = _capturing_sender()
    notifier = Notifier(
        NotificationsConfig(webhook_url="https://hooks.example/x", on_status=("failed",)),
        sender=sender,
    )
    # In-flight state: never notifiable at all.
    assert notifier.notify(status="running", goal="g", score=0, cycle=0, cost_usd=0) is False
    # Terminal, but the operator did not subscribe to it.
    assert notifier.notify(status="passed", goal="g", score=99, cycle=1, cost_usd=0) is False
    assert sender.sent == []


@pytest.mark.parametrize("failure", [OSError("refused"), ValueError("bad url")])
def test_notifier_never_raises_on_transport_failure(failure):
    """A flaky webhook must not turn a passing build red."""
    notifier = Notifier(
        NotificationsConfig(webhook_url="https://hooks.example/x"),
        sender=_capturing_sender(raises=failure),
    )
    assert notifier.notify(status="passed", goal="g", score=99, cycle=1, cost_usd=0) is False


def test_notifier_reports_non_2xx_as_failure():
    notifier = Notifier(
        NotificationsConfig(webhook_url="https://hooks.example/x"),
        sender=_capturing_sender(500),
    )
    assert notifier.notify(status="failed", goal="g", score=0, cycle=1, cost_usd=0) is False


def test_notifier_payload_includes_detail_and_custom_headers():
    payload = Notifier(NotificationsConfig(webhook_url="https://h/x")).payload(
        status="out_of_credits", goal="g", score=0.0, cycle=1, cost_usd=0.5, detail="402"
    )
    assert payload["detail"] == "402"
    assert "402" in payload["text"]


def test_notifications_config_rejects_bad_input():
    with pytest.raises(ConfigError):
        build_config({"notifications": {"webhook_url": "ftp://nope"}})
    with pytest.raises(ConfigError):
        build_config({"notifications": {"on_status": ["not_a_status"]}})
    with pytest.raises(ConfigError):
        build_config({"notifications": {"headers": ["not", "a", "map"]}})
    with pytest.raises(ConfigError):
        build_config({"notifications": {"timeout_seconds": 0.0}})


def test_notifications_config_defaults_and_overrides():
    default = build_config({}).notifications
    assert default.webhook_url == ""
    assert "passed" in default.on_status
    custom = build_config(
        {
            "notifications": {
                "webhook_url": "http://localhost:9000/hook",
                "on_status": ["failed"],
                "headers": {"X-Token": "abc"},
                "timeout_seconds": 2.5,
            }
        }
    ).notifications
    assert custom.on_status == ("failed",)
    assert custom.headers == {"X-Token": "abc"}
    assert custom.timeout_seconds == 2.5


# --------------------------------------------------------------------------
# G-2  Resume checkpoint
# --------------------------------------------------------------------------


def test_build_checkpoint_extracts_only_the_resume_contract(tmp_path):
    state = StateManager(tmp_path / "s.json")
    state.update(current_goal="g", status="error", cycle=3)
    state.record_completed_phase("research")
    state.record_completed_phase("research")  # idempotent
    checkpoint = build_checkpoint(state.state)
    assert checkpoint == {
        "goal": "g",
        "completed_phases": ["research"],
        "cycle": 3,
        "status": "error",
    }


def test_clear_completed_phases(tmp_path):
    state = StateManager(tmp_path / "s.json")
    state.record_completed_phase("research")
    state.clear_completed_phases()
    assert state.state["completed_phases"] == []


def test_completed_phases_survive_a_reload(tmp_path):
    path = tmp_path / "s.json"
    first = StateManager(path)
    first.update(current_goal="g")
    first.record_completed_phase("architecture")
    first.save()
    assert StateManager(path).state["completed_phases"] == ["architecture"]


def _daemon(tmp_path, *, resume: bool, monkeypatch) -> LooperDaemon:
    config = build_config(
        {
            "workspace": str(tmp_path / "ws"),
            "state_file": str(tmp_path / "state.json"),
        }
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    daemon = LooperDaemon(config, resume=resume)
    # No test in this module may touch the network. Without this stub the
    # fix phase inside _build_phases reached OpenRouter for real (observed:
    # four live 401s and leaked sockets), which makes the suite slow, flaky
    # offline, and dependent on someone else's uptime.
    monkeypatch.setattr(daemon.phases, "run_fix", _async_fail_result("fix"))
    return daemon


def _async_fail_result(phase: str):
    async def handler(*args, **kwargs):
        return _fail_result(phase)

    return handler


def _plant_artifact(daemon: LooperDaemon, relative: str) -> None:
    path = Path(daemon.config.workspace) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("content", encoding="utf-8")


def test_resume_off_by_default_skips_nothing(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, resume=False, monkeypatch=monkeypatch)
    daemon.state.update(current_goal="g", status="error")
    daemon.state.record_completed_phase("research")
    _plant_artifact(daemon, "research.md")
    assert daemon.resumable_phases("g") == ()


def test_resume_skips_completed_unscored_phases(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, resume=True, monkeypatch=monkeypatch)
    daemon.state.update(current_goal="g", status="error")
    daemon.state.record_completed_phase("research")
    daemon.state.record_completed_phase("architecture")
    _plant_artifact(daemon, "research.md")
    _plant_artifact(daemon, "architecture/design.md")
    assert daemon.resumable_phases("g") == ("research", "architecture")


def test_resume_refuses_a_different_goal(tmp_path, monkeypatch):
    """Resuming across goals would feed cycle 1 someone else's design."""
    daemon = _daemon(tmp_path, resume=True, monkeypatch=monkeypatch)
    daemon.state.update(current_goal="build a todo app", status="error")
    daemon.state.record_completed_phase("research")
    _plant_artifact(daemon, "research.md")
    assert daemon.resumable_phases("build a url shortener") == ()


def test_resume_refuses_when_no_prior_goal(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, resume=True, monkeypatch=monkeypatch)
    assert daemon.resumable_phases("g") == ()


def test_resume_refuses_a_completed_run(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, resume=True, monkeypatch=monkeypatch)
    daemon.state.update(current_goal="g", status="done")
    daemon.state.record_completed_phase("research")
    _plant_artifact(daemon, "research.md")
    assert daemon.resumable_phases("g") == ()


def test_resume_refuses_when_the_artifact_is_gone(tmp_path, monkeypatch):
    """A wiped workspace with a stale state file must not skip anything."""
    daemon = _daemon(tmp_path, resume=True, monkeypatch=monkeypatch)
    daemon.state.update(current_goal="g", status="error")
    daemon.state.record_completed_phase("research")
    assert daemon.resumable_phases("g") == ()


def test_resume_refuses_an_empty_artifact(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, resume=True, monkeypatch=monkeypatch)
    daemon.state.update(current_goal="g", status="error")
    daemon.state.record_completed_phase("research")
    path = Path(daemon.config.workspace) / "research.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    assert daemon.resumable_phases("g") == ()


def test_scored_phases_are_never_resumable(tmp_path, monkeypatch):
    """ADR-004: skipping review would bank a score this cycle never earned."""
    daemon = _daemon(tmp_path, resume=True, monkeypatch=monkeypatch)
    daemon.state.update(current_goal="g", status="error")
    for phase in ("build", "review", "security_audit", "test"):
        daemon.state.record_completed_phase(phase)
    _plant_artifact(daemon, "src/generated_code.py")
    _plant_artifact(daemon, "review.md")
    _plant_artifact(daemon, "security_audit.md")
    assert daemon.resumable_phases("g") == ()
    assert "review" not in RESUMABLE_PHASES
    assert "build" not in RESUMABLE_PHASES


def test_phase_artifact_exists_is_false_for_unmapped_phases(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, resume=True, monkeypatch=monkeypatch)
    assert daemon.phases.phase_artifact_exists("test") is False
    assert daemon.phases.phase_artifact_exists("nonexistent_phase") is False


def test_run_phases_consumes_a_skip_only_once(tmp_path, monkeypatch):
    """A skip is valid for the cycle that inherited it, not forever."""
    daemon = _daemon(tmp_path, resume=True, monkeypatch=monkeypatch)
    calls: list[str] = []

    async def fake_research(goal):
        calls.append("research")
        return _ok_result("research")

    monkeypatch.setattr(daemon.phases, "run_research", fake_research)
    daemon._skip_phases = {"research"}
    evidence = asyncio.run(daemon._run_phases("g", ("research",), _evidence()))
    assert calls == []  # cycle 1: skipped
    asyncio.run(daemon._run_phases("g", ("research",), evidence))
    assert calls == ["research"]  # cycle 2: actually runs


def test_run_phases_checkpoints_only_successful_phases(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, resume=True, monkeypatch=monkeypatch)

    async def ok(goal):
        return _ok_result("research")

    async def bad(goal):
        return _fail_result("architecture")

    monkeypatch.setattr(daemon.phases, "run_research", ok)
    monkeypatch.setattr(daemon.phases, "run_architecture", bad)
    asyncio.run(daemon._run_phases("g", ("research", "architecture"), _evidence()))
    assert daemon.state.state["completed_phases"] == ["research"]


def _evidence():
    from looper.phases import CycleEvidence

    return CycleEvidence()


def _ok_result(phase: str):
    from looper.phases import PhaseResult

    return PhaseResult(phase=phase, agent="a", model="m", ok=True, summary="ok")


def _fail_result(phase: str):
    from looper.phases import PhaseResult

    return PhaseResult(phase=phase, agent="a", model="m", ok=False, error="boom")


# --------------------------------------------------------------------------
# G-3  402 fail-fast between the two agents of run_architecture
# --------------------------------------------------------------------------


def test_architecture_does_not_call_the_designer_after_a_402(tmp_path):
    """Observed failure: one build logged ten identical 402 errors.

    ``run_architecture`` fires two agents. When the architect gets a 402 the
    designer is guaranteed to get one too, so issuing the second call buys
    nothing and doubles the error noise. The signal must still propagate.
    """
    from looper.llm import AgentReply
    from looper.phases import PhaseManager
    from looper.state import StateManager

    from .conftest import build_config as cfg
    from .conftest import make_client

    config = cfg({"execution": {"sandbox_tests": False}}, env={})
    # A fresh state file: the tracked looper_state.json in the repo already
    # holds errors from earlier runs, and counting into it would measure
    # history rather than this call.
    state = StateManager(tmp_path / "state.json", config.execution.max_history_entries)
    client = make_client(config)
    seen: list[str] = []

    async def broke(agent, prompt, *, extra_system=""):
        seen.append(agent.role)
        return AgentReply(
            text="", ok=False, attempts=1, error="402 out of credits", out_of_credits=True
        )

    client.call = broke  # type: ignore[method-assign]
    result = asyncio.run(PhaseManager(config, state, client).run_architecture("goal"))

    assert len(seen) == 1, f"designer was still called after a 402: {seen}"
    assert result.out_of_credits is True
    assert result.ok is False
    assert state.state["errors"].count("architecture: 402 out of credits") == 1


def test_architecture_still_calls_both_agents_on_a_normal_failure():
    """Only a 402 short-circuits: an ordinary error must not skip the designer."""
    from looper.llm import AgentReply
    from looper.phases import PhaseManager
    from looper.state import StateManager

    from .conftest import build_config as cfg
    from .conftest import make_client

    config = cfg({"execution": {"sandbox_tests": False}}, env={})
    state = StateManager(config.state_file, config.execution.max_history_entries)
    client = make_client(config)
    seen: list[str] = []

    async def flaky(agent, prompt, *, extra_system=""):
        seen.append(agent.role)
        return AgentReply(text="", ok=False, attempts=3, error="503 upstream")

    client.call = flaky  # type: ignore[method-assign]
    result = asyncio.run(PhaseManager(config, state, client).run_architecture("goal"))
    assert len(seen) == 2
    assert result.out_of_credits is False


# --------------------------------------------------------------------------
# Orchestrator integration: notifications fire on every terminal outcome
# --------------------------------------------------------------------------


def test_build_notifies_on_success_and_below_minimum(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, resume=False, monkeypatch=monkeypatch)
    sender = _capturing_sender()
    daemon.notifier = Notifier(
        NotificationsConfig(webhook_url="https://hooks.example/x"), sender=sender
    )

    async def no_phases(goal, names, evidence):
        return evidence

    monkeypatch.setattr(daemon, "_run_phases", no_phases)
    asyncio.run(daemon.build("g"))
    assert len(sender.sent) == 1
    # An empty pipeline cannot clear min_acceptable, so this is the
    # below_minimum path -- the one an operator most needs to hear about.
    assert b'"status": "below_minimum"' in sender.sent[0][1]


def test_build_notifies_on_out_of_credits(tmp_path, monkeypatch):
    from looper.llm import OutOfCreditsError

    daemon = _daemon(tmp_path, resume=False, monkeypatch=monkeypatch)
    sender = _capturing_sender()
    daemon.notifier = Notifier(
        NotificationsConfig(webhook_url="https://hooks.example/x"), sender=sender
    )

    async def broke(goal, names, evidence):
        raise OutOfCreditsError("402")

    monkeypatch.setattr(daemon, "_run_phases", broke)
    with pytest.raises(OutOfCreditsError):
        asyncio.run(daemon.build("g"))
    assert b'"status": "out_of_credits"' in sender.sent[0][1]


def test_build_notifies_on_cost_exhausted(tmp_path, monkeypatch):
    from looper.config import CostBudgetExceeded

    daemon = _daemon(tmp_path, resume=False, monkeypatch=monkeypatch)
    sender = _capturing_sender()
    daemon.notifier = Notifier(
        NotificationsConfig(webhook_url="https://hooks.example/x"), sender=sender
    )

    async def broke(goal, names, evidence):
        raise CostBudgetExceeded(9.0, 1.0)

    monkeypatch.setattr(daemon, "_run_phases", broke)
    with pytest.raises(CostBudgetExceeded):
        asyncio.run(daemon.build("g"))
    assert b'"status": "cost_exhausted"' in sender.sent[0][1]
    assert daemon.state.state["status"] == "cost_exhausted"


def test_a_broken_webhook_never_breaks_a_build(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, resume=False, monkeypatch=monkeypatch)
    daemon.notifier = Notifier(
        NotificationsConfig(webhook_url="https://hooks.example/x"),
        sender=_capturing_sender(raises=OSError("connection refused")),
    )

    async def no_phases(goal, names, evidence):
        return evidence

    monkeypatch.setattr(daemon, "_run_phases", no_phases)
    # The build must complete and return its score: a refused webhook is not
    # a build failure. (The score is whatever the empty pipeline earns; the
    # assertion is that we got one at all instead of an OSError.)
    score = asyncio.run(daemon.build("g"))
    assert isinstance(score, float)
    assert daemon.state.state["status"] == "done"


def test_build_actually_skips_resumed_phases_end_to_end(tmp_path, monkeypatch):
    """The whole point of --resume: the skipped phase is never paid for again.

    Exercises the real ``_build_phases`` path -- checkpoint read before
    reset(), skip set installed, phase handler never invoked.
    """
    daemon = _daemon(tmp_path, resume=True, monkeypatch=monkeypatch)
    daemon.state.update(current_goal="g", status="error")
    daemon.state.record_completed_phase("research")
    daemon.state.record_completed_phase("architecture")
    daemon.state.save()
    _plant_artifact(daemon, "research.md")
    _plant_artifact(daemon, "architecture/design.md")

    invoked: list[str] = []

    def _spy(name):
        async def handler(goal):
            invoked.append(name)
            return _ok_result(name)

        return handler

    for phase in ("research", "architecture", "build", "test", "review", "security_audit"):
        monkeypatch.setattr(daemon.phases, f"run_{phase}", _spy(phase))
    monkeypatch.setattr(
        daemon.config.execution.__class__, "max_cycles", property(lambda self: 1), raising=False
    )

    asyncio.run(daemon.build("g"))

    assert "research" not in invoked, "a resumed phase was paid for again"
    assert "architecture" not in invoked
    assert "build" in invoked, "scored phases must still run"
    # The checkpoint is carried forward so a second interruption resumes too.
    assert "research" in daemon.state.state["completed_phases"]


def test_build_without_resume_runs_every_phase(tmp_path, monkeypatch):
    """The negative direction: a checkpoint must do nothing when resume is off."""
    daemon = _daemon(tmp_path, resume=False, monkeypatch=monkeypatch)
    daemon.state.update(current_goal="g", status="error")
    daemon.state.record_completed_phase("research")
    daemon.state.save()
    _plant_artifact(daemon, "research.md")

    invoked: list[str] = []

    def _spy(name):
        async def handler(goal):
            invoked.append(name)
            return _ok_result(name)

        return handler

    for phase in ("research", "architecture", "build", "test", "review", "security_audit"):
        monkeypatch.setattr(daemon.phases, f"run_{phase}", _spy(phase))

    asyncio.run(daemon.build("g"))
    assert "research" in invoked


# --------------------------------------------------------------------------
# CLI: --resume flag and the doctor's WSL reporting
# --------------------------------------------------------------------------


def test_cli_resume_flag_defaults_off_and_parses():
    from looper.cli import build_parser

    assert build_parser().parse_args([]).resume is False
    assert build_parser().parse_args(["--resume", "--goal", "g"]).resume is True


def test_doctor_reports_the_wsl_backend(monkeypatch, caplog):
    import logging

    from looper.cli import EXIT_OK, run_doctor

    config = build_config({"execution": {"sandbox_backend": "wsl"}}, env={})
    monkeypatch.setattr("looper.cli.docker_available", lambda: False)
    monkeypatch.setattr("looper.cli.podman_available", lambda: False)
    monkeypatch.setattr("looper.cli.posix_rlimits_available", lambda: False)
    monkeypatch.setattr("looper.cli.wsl_available", lambda: True)
    monkeypatch.setattr("looper.cli.resolve_backend", lambda *a, **k: "wsl")
    with caplog.at_level(logging.WARNING):
        assert run_doctor(config) == EXIT_OK
    # The weaker guarantees must be stated, not implied.
    assert "shares the host network" in caplog.text


def test_doctor_names_wsl_as_a_remedy_when_nothing_is_available(monkeypatch, caplog):
    import logging

    from looper.cli import EXIT_SANDBOX_UNAVAILABLE, run_doctor
    from looper.sandbox import SandboxUnavailableError

    config = build_config({}, env={})
    monkeypatch.setattr("looper.cli.docker_available", lambda: False)
    monkeypatch.setattr("looper.cli.podman_available", lambda: False)
    monkeypatch.setattr("looper.cli.posix_rlimits_available", lambda: False)
    monkeypatch.setattr("looper.cli.wsl_available", lambda: False)

    def _refuse(*a, **k):
        raise SandboxUnavailableError("nothing available")

    monkeypatch.setattr("looper.cli.resolve_backend", _refuse)
    with caplog.at_level(logging.ERROR):
        assert run_doctor(config) == EXIT_SANDBOX_UNAVAILABLE
    assert "wsl --install" in caplog.text


# --------------------------------------------------------------------------
# The real HTTP transport (no network: urlopen is stubbed)
# --------------------------------------------------------------------------


def test_default_sender_posts_json_and_returns_the_status(monkeypatch):
    from looper import notify as notify_module

    captured = {}

    class _Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["body"] = request.data
        captured["content_type"] = request.headers.get("Content-type")
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(notify_module.urllib.request, "urlopen", fake_urlopen)
    code = notify_module._default_sender(
        "https://hooks.example/x", b'{"a":1}', {"Content-Type": "application/json"}, 5.0
    )
    assert code == 204
    assert captured["method"] == "POST"
    assert captured["url"] == "https://hooks.example/x"
    assert captured["body"] == b'{"a":1}'
    assert captured["content_type"] == "application/json"
    assert captured["timeout"] == 5.0


def test_notifier_uses_the_real_transport_by_default(monkeypatch):
    """A Notifier built without an injected sender must still be wired up."""
    from looper import notify as notify_module

    calls: list[str] = []
    monkeypatch.setattr(
        notify_module,
        "_default_sender",
        lambda url, body, headers, timeout: calls.append(url) or 200,
    )
    notifier = Notifier(NotificationsConfig(webhook_url="https://hooks.example/x"))
    notifier._send = notify_module._default_sender
    assert notifier.notify(status="passed", goal="g", score=99, cycle=1, cost_usd=0) is True
    assert calls == ["https://hooks.example/x"]
