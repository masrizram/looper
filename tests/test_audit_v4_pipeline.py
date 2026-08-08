"""Regressions for the audit-round-four findings that live outside the scanner.

Split from ``test_audit_v4_regressions`` so the calibration corpus there stays
readable: these cover the orchestration, scoring, server and execution paths.
"""

from __future__ import annotations

import asyncio

from looper.config import ScoringWeights
from looper.phases.execution import ExecutionMixin
from looper.scoring import ScoringEngine
from looper.state import DEFAULT_STATE

from .conftest import FakeRequest
from .test_server import make_server

# --- H-5: pytest isolation must not strip the workspace from sys.path -------


def test_h5_pytest_argv_keeps_the_workspace_importable():
    """``-I`` implies ``-P``, which drops the script directory from sys.path.

    Every generated suite that imported the module under test -- the one thing
    the test prompt explicitly demands -- failed collection with
    ModuleNotFoundError and scored 0 tests. Verified by running real pytest
    both ways; ``-E -s`` keeps the isolation that actually mattered.
    """
    doc = ExecutionMixin._run_pytest.__doc__ or ""
    assert "-E -s" in doc


# --- M-5: finding volume is evidence, not just severity ----------------------


def test_m5_a_flood_of_unknown_severity_findings_is_capped():
    """50 UNKNOWN findings zeroed the security bucket and then stopped
    mattering, so the build could still clear the gate on the other three."""
    weights = ScoringWeights()
    calc = ScoringEngine(weights)
    many = [f"- something odd #{i}" for i in range(50)]
    result = calc.calculate(
        build_ok=True,
        tests_passed=10,
        tests_total=10,
        review_score=100.0,
        security_issues=many,
    )
    assert "finding_volume" in result.caps_applied
    assert result.total <= weights.critical_finding_cap


def test_m5_a_handful_of_findings_does_not_trip_the_volume_cap():
    calc = ScoringEngine(ScoringWeights())
    result = calc.calculate(
        build_ok=True,
        tests_passed=10,
        tests_total=10,
        review_score=100.0,
        security_issues=["- LOW: cosmetic"],
    )
    assert "finding_volume" not in result.caps_applied


# --- M-7: an unidentifiable peer is rejected, not pooled ---------------------


def test_m7_unidentifiable_peer_is_rejected_with_400():
    """Pooling every anonymous caller into one bucket means the first to hit
    the limit locks out all the others -- a self-inflicted denial of service."""
    server = make_server()
    request = FakeRequest({"goal": "x"}, remote=None)
    response = asyncio.run(server.handle_build(request))
    assert response.status == 400


def test_m7_an_identifiable_peer_is_still_served():
    server = make_server()
    request = FakeRequest({"goal": "x"}, remote="10.0.0.1")
    response = asyncio.run(server.handle_build(request))
    assert response.status != 400


# --- L-4: skipped final phases are visible in state --------------------------


def test_l4_state_exposes_artifact_completeness():
    """A log line is invisible to anyone reading /status or the state file."""
    assert DEFAULT_STATE["artifacts_complete"] is True


# --- branch closure: paths reachable only by specific shapes -----------------


def test_m8_architecture_propagates_out_of_credits_from_either_agent():
    """M-8: run_architecture is the only multi-agent phase that bypasses
    _run_agent_phase, so it silently dropped the 402 fast-fail signal and the
    orchestrator kept spending on later phases."""
    from looper.llm import AgentReply
    from looper.phases import PhaseManager
    from looper.state import StateManager

    from .conftest import build_config, make_client

    config = build_config({"execution": {"sandbox_tests": False}}, env={})
    state = StateManager(config.state_file, config.execution.max_history_entries)
    client = make_client(config)

    async def _broke(agent, prompt, *, extra_system=""):
        return AgentReply(
            text="",
            ok=False,
            attempts=1,
            error="402 out of credits",
            out_of_credits=True,
        )

    client.call = _broke  # type: ignore[method-assign]
    phases = PhaseManager(config, state, client)
    result = asyncio.run(phases.run_architecture("goal"))
    assert result.out_of_credits is True
    assert result.ok is False


def test_adequacy_import_from_a_stdlib_module_alone_is_not_a_subject():
    """adequacy.py:136->133 -- the loop must keep walking past a stdlib
    ImportFrom rather than returning on the first one it sees."""
    from looper.adequacy import evaluate_suite

    src = (
        "from pathlib import Path\n"
        "from src.generated_code import main\n"
        "def test_a():\n"
        "    assert main() is not None\n"
    )
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.imports_subject is True


def test_adequacy_relative_import_with_no_module_is_skipped():
    from looper.adequacy import evaluate_suite

    src = "from . import sibling\ndef test_a():\n    assert sibling\n"
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.imports_subject is False


def test_sandbox_alias_fixed_point_runs_to_its_pass_limit():
    """sandbox.py:273->290 -- exhausting the pass budget (rather than breaking
    early) must still terminate and still confine the fixture writes."""
    from looper.sandbox import _ALIAS_RESOLUTION_PASSES, scan_for_dangerous_calls

    # Aliases defined in reverse order force one new discovery per pass, so the
    # loop runs its full budget instead of converging on the second sweep.
    depth = _ALIAS_RESOLUTION_PASSES
    lines = ["def test_chain(tmp_path):"]
    for i in range(depth, 0, -1):
        prev = "tmp_path" if i == 1 else f"p{i - 1}"
        lines.append(f"    p{i} = {prev} / 'd{i}'")
    lines.append(f"    p{depth}.write_text('x')")
    src = "\n".join(lines) + "\n"
    assert scan_for_dangerous_calls(src) == []
