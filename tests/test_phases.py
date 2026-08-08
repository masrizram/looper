"""Phase execution: containment, subprocess hardening, failure semantics."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from looper.config import build_config
from looper.phases import (
    CODE_FILE,
    DESIGN_FILE,
    OPTIMIZED_FILE,
    CycleEvidence,
    PhaseManager,
    PhaseResult,
    WorkspaceEscapeError,
)
from looper.state import StateManager

from .conftest import DEFAULT_REPLIES, make_client


def build_phases(config, replies=None, fail_with=None) -> PhaseManager:
    state = StateManager(config.state_file, config.execution.max_history_entries)
    client = make_client(config, replies, fail_with)
    return PhaseManager(config, state, client)


# --- PhaseResult contract (A-4) ---------------------------------------------


def test_phase_result_defaults_are_pessimistic():
    """A-4: the old dict contract used .get('build_ok', True), so a phase that
    forgot the key was silently treated as a success."""
    result = PhaseResult(phase="build", agent="A", model="m")
    assert result.ok is False
    assert result.build_ok is False
    assert result.status == "error"
    assert result.tests_total == 0
    assert result.review_score == 0.0
    assert result.security_issues == ()


def test_phase_result_serialises():
    payload = PhaseResult(phase="build", agent="A", model="m", ok=True).as_dict()
    assert payload["status"] == "done"
    assert payload["phase"] == "build"


def test_phase_result_is_frozen():
    result = PhaseResult(phase="p", agent="a", model="m")
    with pytest.raises(Exception):
        result.phase = "other"


# --- Workspace containment (H-3) --------------------------------------------


@pytest.mark.parametrize(
    "evil",
    ["../escape.py", "../../etc/passwd", "src/../../outside.py", "a/b/../../../x.py"],
)
def test_write_file_rejects_traversal(config, evil):
    phases = build_phases(config)
    with pytest.raises(WorkspaceEscapeError):
        phases.write_file(evil, "pwned")


def test_read_file_rejects_traversal(config):
    phases = build_phases(config)
    with pytest.raises(WorkspaceEscapeError):
        phases.read_file("../../secrets.txt")


def test_nested_paths_inside_workspace_are_allowed(config):
    phases = build_phases(config)
    written = phases.write_file("a/b/c/deep.py", "x=1")
    assert "deep.py" in written
    assert phases.read_file("a/b/c/deep.py") == "x=1"


def test_read_missing_file_returns_empty(config):
    assert build_phases(config).read_file("nope.md") == ""


def test_write_records_file_in_state(config):
    phases = build_phases(config)
    phases.write_file("x.md", "hi")
    assert any("x.md" in p for p in phases.state.state["files_created"])


# --- Individual phases -------------------------------------------------------


def test_research_writes_file_and_succeeds(config):
    phases = build_phases(config)
    result = asyncio.run(phases.run_research("goal"))
    assert result.ok is True
    assert result.summary == "Research completed"
    assert phases.read_file("research.md") == "research notes"


def test_architecture_combines_both_agents(config):
    phases = build_phases(config)
    result = asyncio.run(phases.run_architecture("goal"))
    assert result.ok is True
    combined = phases.read_file(DESIGN_FILE)
    assert "architecture notes" in combined
    assert "api notes" in combined
    assert "UX/API Designer" in result.agent


def test_architecture_fails_if_either_agent_fails(config):
    phases = build_phases(config, fail_with=RuntimeError("down"))
    result = asyncio.run(phases.run_architecture("goal"))
    assert result.ok is False
    assert "down" in result.error


def test_build_marks_build_ok(config):
    phases = build_phases(config)
    result = asyncio.run(phases.run_build("goal"))
    assert result.build_ok is True
    assert phases.read_file(CODE_FILE) == "print('hello')"


def test_build_failure_sets_build_ok_false(config):
    phases = build_phases(config, fail_with=RuntimeError("no api"))
    result = asyncio.run(phases.run_build("goal"))
    assert result.build_ok is False
    assert result.ok is False
    assert phases.state.state["errors"]


# --- Review ------------------------------------------------------------------


def test_review_extracts_score(config):
    phases = build_phases(config)
    result = asyncio.run(phases.run_review("goal"))
    assert result.review_score == 97.0


def test_review_failure_scores_zero(config):
    """A failed reviewer must never read as a pass."""
    phases = build_phases(config, fail_with=RuntimeError("outage"))
    result = asyncio.run(phases.run_review("goal"))
    assert result.review_score == 0.0
    assert result.ok is False


def test_review_without_score_line_scores_zero(config, caplog):
    replies = {**DEFAULT_REPLIES, "Senior Reviewer": "Looks good to me!"}
    phases = build_phases(config, replies)
    result = asyncio.run(phases.run_review("goal"))
    assert result.review_score == 0.0
    assert "no 'Score:" in caplog.text


def test_review_score_is_clamped_to_100(config):
    replies = {**DEFAULT_REPLIES, "Senior Reviewer": "Score: 500"}
    phases = build_phases(config, replies)
    assert asyncio.run(phases.run_review("g")).review_score == 100.0


# --- Security audit (C-1) ----------------------------------------------------


def test_security_audit_extracts_real_findings(config):
    """C-1 end-to-end: real findings must reach the score, not a placeholder."""
    replies = {
        **DEFAULT_REPLIES,
        "Security Auditor": "- CRITICAL: hardcoded AWS key\n- LOW: verbose errors\n",
    }
    phases = build_phases(config, replies)
    result = asyncio.run(phases.run_security_audit("goal"))
    assert list(result.security_issues) == [
        "CRITICAL: hardcoded AWS key",
        "LOW: verbose errors",
    ]


def test_security_audit_no_issues(config):
    phases = build_phases(config)
    assert asyncio.run(phases.run_security_audit("goal")).security_issues == ()


def test_security_audit_failure_is_blocking(config):
    """An agent outage must never read as 'no issues found'."""
    phases = build_phases(config, fail_with=RuntimeError("outage"))
    result = asyncio.run(phases.run_security_audit("goal"))
    assert list(result.security_issues) == ["CRITICAL: security audit did not complete"]


def test_security_audit_unparseable_output_is_flagged(config):
    replies = {**DEFAULT_REPLIES, "Security Auditor": "I think it's probably fine?"}
    phases = build_phases(config, replies)
    result = asyncio.run(phases.run_security_audit("goal"))
    assert list(result.security_issues) == ["MEDIUM: audit output not in expected format"]


# --- Test phase & subprocess hardening (H-5) ---------------------------------


def test_run_test_parses_summary(config, stub_pytest_run):
    stub_pytest_run("2 passed in 0.01s")
    phases = build_phases(config)
    result = asyncio.run(phases.run_test("goal"))
    assert (result.tests_passed, result.tests_total) == (2, 2)
    assert result.ok is True


def test_run_test_counts_failures(config, stub_pytest_run):
    stub_pytest_run("1 passed, 3 failed in 0.1s", returncode=1)
    phases = build_phases(config)
    result = asyncio.run(phases.run_test("goal"))
    assert (result.tests_passed, result.tests_total) == (1, 4)


def test_run_test_timeout_is_a_failure_not_a_hang(config, monkeypatch):
    """H-5: the old call had no timeout, so one `while True:` in generated
    code wedged a 24/7 daemon forever."""

    async def fake_to_thread(func, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    phases = build_phases(config)
    result = asyncio.run(phases.run_test("goal"))
    assert result.tests_total == 1
    assert result.tests_passed == 0
    assert "timed out" in result.summary


def test_run_test_handles_spawn_failure(config, monkeypatch):
    async def fake_to_thread(func, *args, **kwargs):
        raise OSError("no interpreter")

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    phases = build_phases(config)
    result = asyncio.run(phases.run_test("goal"))
    assert "could not run tests" in result.summary


def test_run_test_collection_error_counts_as_failure(config, stub_pytest_run):
    stub_pytest_run(stdout="", stderr="", returncode=2)
    phases = build_phases(config)
    result = asyncio.run(phases.run_test("goal"))
    assert result.tests_total == 1
    assert "no test summary" in result.summary


def test_run_test_skips_execution_when_agent_failed(config, monkeypatch):
    called = False

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    phases = build_phases(config, fail_with=RuntimeError("outage"))
    result = asyncio.run(phases.run_test("goal"))
    assert result.tests_total == 0
    assert called is False


def test_subprocess_argv_is_hardened(config, monkeypatch):
    """Fixed argv, isolated mode, no cache writes, hard timeout."""
    captured = {}

    async def fake_to_thread(func, argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs

        class Proc:
            stdout = "1 passed"
            stderr = ""
            returncode = 0

        return Proc()

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    phases = build_phases(config)
    asyncio.run(phases.run_test("goal"))

    argv = captured["argv"]
    assert "-I" in argv  # isolated from user site-packages
    assert "-B" in argv  # no bytecode writes
    assert "no:cacheprovider" in argv
    assert captured["kwargs"]["timeout"] == config.execution.test_timeout_seconds
    assert captured["kwargs"]["check"] is False
    assert "shell" not in captured["kwargs"]  # never shell=True


# --- Optimize / documentation / fix ------------------------------------------


def test_optimize_writes_a_separate_file(config):
    """The optimizer's rewrite has not been re-reviewed, so it must not
    overwrite the canonical generated code."""
    phases = build_phases(config)
    phases.write_file(CODE_FILE, "original")
    asyncio.run(phases.run_performance_optimize("goal"))
    assert phases.read_file(CODE_FILE) == "original"
    assert phases.read_file(OPTIMIZED_FILE) == "optimized"


def test_documentation_prefers_optimized_code(config):
    phases = build_phases(config)
    phases.write_file(CODE_FILE, "generated")
    phases.write_file(OPTIMIZED_FILE, "optimized-src")
    asyncio.run(phases.run_documentation("goal"))
    prompt = phases.client._client.completions.calls[-1]["messages"][1]["content"]
    assert "optimized-src" in prompt


def test_documentation_falls_back_to_generated(config):
    phases = build_phases(config)
    phases.write_file(CODE_FILE, "generated-only")
    asyncio.run(phases.run_documentation("goal"))
    prompt = phases.client._client.completions.calls[-1]["messages"][1]["content"]
    assert "generated-only" in prompt


def test_fix_promotes_patch_to_canonical_code(config):
    phases = build_phases(config)
    phases.write_file(CODE_FILE, "broken")
    result = asyncio.run(phases.run_fix("goal", ["HIGH: bug"]))
    assert result.build_ok is True
    assert phases.read_file(CODE_FILE) == "print('fixed')"
    assert len(result.files_created) == 2  # archive + canonical


def test_fix_failure_does_not_overwrite_code(config):
    phases = build_phases(config, fail_with=RuntimeError("down"))
    phases.write_file(CODE_FILE, "original")
    result = asyncio.run(phases.run_fix("goal", ["HIGH: bug"]))
    assert result.build_ok is False
    assert phases.read_file(CODE_FILE) == "original"


def test_fix_archives_per_cycle(config):
    phases = build_phases(config)
    phases.state.update(cycle=3)
    result = asyncio.run(phases.run_fix("goal", ["x"]))
    assert any("fixes_cycle_3" in f for f in result.files_created)


def test_optimize_failure_reports_error(config):
    phases = build_phases(config, fail_with=RuntimeError("nope"))
    result = asyncio.run(phases.run_performance_optimize("goal"))
    assert result.ok is False
    assert "nope" in result.error


def test_documentation_failure_reports_error(config):
    phases = build_phases(config, fail_with=RuntimeError("nope"))
    assert asyncio.run(phases.run_documentation("goal")).ok is False


# --- CycleEvidence -----------------------------------------------------------


def test_evidence_absorbs_each_phase():
    evidence = CycleEvidence()
    evidence.absorb(PhaseResult(phase="build", agent="a", model="m", ok=True, build_ok=True))
    evidence.absorb(PhaseResult(phase="test", agent="a", model="m", tests_passed=3, tests_total=4))
    evidence.absorb(PhaseResult(phase="review", agent="a", model="m", review_score=88.0))
    evidence.absorb(
        PhaseResult(phase="security_audit", agent="a", model="m", security_issues=("HIGH: x",))
    )
    assert evidence.build_ok is True
    assert (evidence.tests_passed, evidence.tests_total) == (3, 4)
    assert evidence.review_score == 88.0
    assert evidence.security_issues == ["HIGH: x"]


def test_evidence_ignores_unrelated_phases():
    evidence = CycleEvidence(build_ok=True)
    evidence.absorb(PhaseResult(phase="research", agent="a", model="m", ok=True))
    assert evidence.build_ok is True


def test_evidence_absorbs_fix_as_build_signal():
    evidence = CycleEvidence(build_ok=False)
    evidence.absorb(PhaseResult(phase="fix", agent="a", model="m", ok=True, build_ok=True))
    assert evidence.build_ok is True


def test_workspace_is_created_on_init(config):
    phases = build_phases(config)
    assert phases.workspace.exists()


# --- File size cap -----------------------------------------------------------


def test_oversized_agent_output_is_truncated(raw_config):
    """An LLM stuck in a loop must not fill the disk of a 24/7 daemon."""
    config = build_config({**raw_config, "execution": {"max_file_bytes": 2048}}, env={})
    phases = build_phases(config)
    phases.write_file("big.py", "x" * 10_000)
    written = phases.read_file("big.py")
    assert len(written.encode("utf-8")) < 10_000
    assert "TRUNCATED by looper" in written


def test_output_within_the_cap_is_untouched(raw_config):
    config = build_config({**raw_config, "execution": {"max_file_bytes": 4096}}, env={})
    phases = build_phases(config)
    phases.write_file("small.py", "print('ok')")
    assert phases.read_file("small.py") == "print('ok')"


def test_truncation_does_not_split_a_utf8_character(raw_config):
    """Slicing bytes mid-codepoint would raise; errors='ignore' prevents it."""
    config = build_config({**raw_config, "execution": {"max_file_bytes": 1025}}, env={})
    phases = build_phases(config)
    phases.write_file("uni.md", "\u00e9" * 2000)  # 2 bytes each
    assert "TRUNCATED by looper" in phases.read_file("uni.md")
