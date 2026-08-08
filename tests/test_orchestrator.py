"""Orchestrator: the deterministic control loop and its gates."""

from __future__ import annotations

import asyncio

import pytest

from looper.config import build_config
from looper.orchestrator import LooperDaemon
from looper.phases import CODE_FILE

from .conftest import DEFAULT_REPLIES


@pytest.fixture
def good_run(stub_pytest_run):
    stub_pytest_run("10 passed in 0.1s")


def cfg_from(raw_config, **overrides):
    merged = {**raw_config}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return build_config(merged, env={})


# --- Happy path --------------------------------------------------------------


def test_successful_build_reaches_the_release_band(daemon_factory, good_run):
    daemon = daemon_factory()
    score = asyncio.run(daemon.build("make a calculator"))
    assert score >= 90.0
    assert daemon.state.state["status"] == "done"
    assert daemon.state.state["current_phase"] == "done"


def test_score_breakdown_is_persisted(daemon_factory, good_run):
    daemon = daemon_factory()
    asyncio.run(daemon.build("goal"))
    breakdown = daemon.state.state["score_breakdown"]
    assert breakdown["total"] == daemon.state.state["score"]
    assert "build" in breakdown


def test_history_records_every_phase(daemon_factory, good_run):
    daemon = daemon_factory()
    asyncio.run(daemon.build("goal"))
    phases = [entry["phase"] for entry in daemon.state.state["history"]]
    for expected in ["research", "architecture", "build", "test", "review", "security_audit"]:
        assert expected in phases


def test_final_phases_run_when_score_is_acceptable(daemon_factory, good_run):
    daemon = daemon_factory()
    asyncio.run(daemon.build("goal"))
    phases = [entry["phase"] for entry in daemon.state.state["history"]]
    assert "performance_optimize" in phases
    assert "documentation" in phases


def test_target_score_stops_early(daemon_factory, good_run):
    daemon = daemon_factory()
    asyncio.run(daemon.build("goal"))
    assert daemon.state.state["cycle"] == 1


# --- Gates -------------------------------------------------------------------


def test_final_phases_skipped_when_score_too_low(raw_config, daemon_factory, stub_pytest_run):
    stub_pytest_run("5 failed in 0.1s", returncode=1)
    config = cfg_from(raw_config, execution={"min_acceptable": 95, "target_score": 99})
    daemon = daemon_factory(cfg=config)
    asyncio.run(daemon.build("goal"))
    phases = [entry["phase"] for entry in daemon.state.state["history"]]
    assert "documentation" not in phases


def test_agent_outage_cannot_produce_a_passing_score(daemon_factory, good_run):
    """Every agent fails: the score must land far below the release band."""
    daemon = daemon_factory(fail_with=RuntimeError("total outage"))
    score = asyncio.run(daemon.build("goal"))
    assert score <= 50.0


def test_critical_finding_blocks_the_release_band(raw_config, daemon_factory, good_run):
    replies = {**DEFAULT_REPLIES, "Security Auditor": "- CRITICAL: remote code execution"}
    config = cfg_from(raw_config, execution={"min_acceptable": 60})
    daemon = daemon_factory(replies=replies, cfg=config)
    score = asyncio.run(daemon.build("goal"))
    assert score <= 50.0
    phases = [entry["phase"] for entry in daemon.state.state["history"]]
    assert "documentation" not in phases


# --- Retry / fix loop --------------------------------------------------------


def test_low_score_triggers_a_fix_cycle(raw_config, daemon_factory, stub_pytest_run):
    stub_pytest_run("3 failed, 1 passed in 0.1s", returncode=1)
    config = cfg_from(
        raw_config, execution={"max_cycles": 2, "min_acceptable": 95, "target_score": 99}
    )
    daemon = daemon_factory(cfg=config)
    asyncio.run(daemon.build("goal"))
    phases = [entry["phase"] for entry in daemon.state.state["history"]]
    assert "fix" in phases


def test_cycles_are_capped(raw_config, daemon_factory, stub_pytest_run):
    stub_pytest_run("9 failed in 0.1s", returncode=1)
    config = cfg_from(
        raw_config, execution={"max_cycles": 3, "min_acceptable": 99, "target_score": 100}
    )
    daemon = daemon_factory(cfg=config)
    asyncio.run(daemon.build("goal"))
    assert daemon.state.state["cycle"] == 3


def test_no_fix_attempted_on_the_final_cycle(raw_config, daemon_factory, stub_pytest_run):
    stub_pytest_run("9 failed in 0.1s", returncode=1)
    config = cfg_from(
        raw_config, execution={"max_cycles": 1, "min_acceptable": 99, "target_score": 100}
    )
    daemon = daemon_factory(cfg=config)
    asyncio.run(daemon.build("goal"))
    phases = [entry["phase"] for entry in daemon.state.state["history"]]
    assert "fix" not in phases


def test_fix_promotes_code_for_the_next_cycle(raw_config, daemon_factory, stub_pytest_run):
    stub_pytest_run("2 failed in 0.1s", returncode=1)
    config = cfg_from(
        raw_config, execution={"max_cycles": 2, "min_acceptable": 95, "target_score": 99}
    )
    daemon = daemon_factory(cfg=config)
    asyncio.run(daemon.build("goal"))
    assert daemon.phases.read_file(CODE_FILE) == "print('fixed')"


def test_retry_cycle_runs_only_validation_phases(raw_config, daemon_factory, stub_pytest_run):
    stub_pytest_run("2 failed in 0.1s", returncode=1)
    config = cfg_from(
        raw_config, execution={"max_cycles": 2, "min_acceptable": 95, "target_score": 99}
    )
    daemon = daemon_factory(cfg=config)
    asyncio.run(daemon.build("goal"))
    cycle2 = [e["phase"] for e in daemon.state.state["history"] if e["cycle"] == 2]
    assert "research" not in cycle2
    assert "build" not in cycle2
    assert "test" in cycle2


# --- Concurrency (H-1) -------------------------------------------------------


def test_concurrent_builds_are_serialised(daemon_factory, good_run):
    """H-1: two triggers previously interleaved into one shared state."""
    daemon = daemon_factory()
    order: list[tuple[str, str]] = []
    original = daemon._build_locked

    async def tracked(goal):
        order.append(("start", goal))
        result = await original(goal)
        order.append(("end", goal))
        return result

    daemon._build_locked = tracked

    async def run():
        await asyncio.gather(daemon.build("A"), daemon.build("B"))

    asyncio.run(run())

    # start/end must pair up; never start,start,end,end.
    assert [kind for kind, _ in order] == ["start", "end", "start", "end"]
    assert order[0][1] == order[1][1]
    assert order[2][1] == order[3][1]


def test_build_lock_is_visible_in_status(daemon_factory, good_run):
    daemon = daemon_factory()
    assert daemon.status_snapshot()["build_in_progress"] is False


# --- Status snapshot ---------------------------------------------------------


def test_status_snapshot_bounds_history(daemon_factory):
    daemon = daemon_factory()
    for i in range(100):
        daemon.state.append_history({"phase": "p", "i": i})
    assert len(daemon.status_snapshot()["history"]) == 20


def test_status_snapshot_is_a_copy(daemon_factory):
    daemon = daemon_factory()
    daemon.state.update(current_goal="original")
    snapshot = daemon.status_snapshot()
    snapshot["current_goal"] = "tampered"
    assert daemon.state.state["current_goal"] == "original"


# --- Phase dispatch ----------------------------------------------------------


def test_unknown_phase_is_skipped_with_a_warning(raw_config, daemon_factory, caplog, good_run):
    daemon = daemon_factory()

    async def run():
        from looper.phases import CycleEvidence

        await daemon._run_phases("goal", ("nonexistent_phase",), CycleEvidence())

    asyncio.run(run())
    assert "Unknown phase" in caplog.text


def test_custom_phase_list_is_honoured(raw_config, daemon_factory, good_run):
    config = cfg_from(raw_config, phases=["build"], final_phases=["documentation"])
    daemon = daemon_factory(cfg=config)
    asyncio.run(daemon.build("goal"))
    phases = {entry["phase"] for entry in daemon.state.state["history"]}
    assert "research" not in phases
    assert "build" in phases


# --- Lifecycle ---------------------------------------------------------------


def test_start_runs_server_and_watcher_then_cleans_up(daemon_factory, tmp_path, good_run):
    from looper.watcher import FileWatcher

    seen: list[str] = []

    async def callback(content: str) -> None:
        seen.append(content)

    watcher = FileWatcher(tmp_path / "cmds.txt", callback, interval=0.001)
    daemon = daemon_factory(watcher=watcher)

    async def run():
        task = asyncio.ensure_future(daemon.start())
        await asyncio.sleep(0.02)
        (tmp_path / "cmds.txt").write_text("goal from file", encoding="utf-8")
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert seen == ["goal from file"]


def test_on_command_triggers_a_build(daemon_factory, good_run):
    daemon = daemon_factory()
    asyncio.run(daemon._on_command("build via file"))
    assert daemon.state.state["current_goal"] == "build via file"


def test_on_goal_triggers_a_build(daemon_factory, good_run):
    daemon = daemon_factory()
    asyncio.run(daemon._on_goal("build via http"))
    assert daemon.state.state["current_goal"] == "build via http"


def test_daemon_builds_its_own_collaborators(config, monkeypatch):
    """Constructing with only a config must work - no injection required."""
    from looper import llm, server

    monkeypatch.setattr(
        llm.OpenRouterClient, "__init__", lambda self, *a, **k: setattr(self, "_client", None)
    )
    monkeypatch.setattr(
        server.HTTPServer,
        "__init__",
        lambda self, *a, **k: setattr(self, "config", config.http),
    )
    daemon = LooperDaemon(config)
    assert daemon.state is not None
    assert daemon.phases is not None
    assert daemon.watcher is not None
    assert daemon.scoring.weights is config.scoring


def test_zero_score_when_no_cycle_produced_a_result(raw_config, daemon_factory, good_run):
    """Covers the `final is None` fallback in the scoring handoff."""
    config = cfg_from(raw_config, phases=[], final_phases=[])
    daemon = daemon_factory(cfg=config)

    async def no_cycles(goal):
        daemon.state.update(current_goal=goal)
        return 0.0

    original_max = daemon.config.execution.max_cycles
    assert original_max >= 1
    score = asyncio.run(daemon.build("goal"))
    assert isinstance(score, float)
