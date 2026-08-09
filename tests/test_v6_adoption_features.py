"""Tests for the v6 adoption features: init, dry-run, report, languages.

These cover the four modules added to close the head-to-head audit's
DevEx and observability gaps. They are behavioural, not line-chasing: each
asserts the property the feature exists for (a starter config that actually
loads; a dry run that spends nothing; a report whose verdict matches the
exit code; an adapter whose argv the pipeline really uses).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from looper.calibration import GateMetrics, evaluate_gate
from looper.cli import main
from looper.config import ConfigError, build_config, load_config_with_dir
from looper.dryrun import STUB_PRICE_USD, StubClient
from looper.languages import (
    DEFAULT_LANGUAGE,
    PythonAdapter,
    adapter_for,
    lint_modes_for,
    supported_languages,
)
from looper.report import build_report, write_run_report, write_step_summary
from looper.scaffold import DEFAULT_CONFIG_NAME, ScaffoldExistsError, write_starter_config

# -- scaffold -----------------------------------------------------------


def test_starter_config_loads_without_further_editing(tmp_path: Path) -> None:
    """The whole point of --init: the file it writes is immediately valid."""
    target = write_starter_config(tmp_path / DEFAULT_CONFIG_NAME)
    config, config_dir = load_config_with_dir(str(target))
    assert config_dir == tmp_path
    assert config.agents, "starter config must define the agent roster"
    assert config.execution.max_cost_usd > 0


def test_starter_config_is_small_enough_to_read() -> None:
    """A 73-key wall of YAML is the barrier; the starter must not be one."""
    from looper.scaffold import STARTER_CONFIG

    top_level = [ln for ln in STARTER_CONFIG.splitlines() if ln and not ln[0].isspace()]
    keys = [ln for ln in top_level if ln.rstrip().endswith(":")]
    assert len(keys) <= 6, f"starter config grew to {len(keys)} sections"


def test_starter_config_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / DEFAULT_CONFIG_NAME
    write_starter_config(target)
    with pytest.raises(ScaffoldExistsError):
        write_starter_config(target)


def test_starter_config_creates_parent_directories(tmp_path: Path) -> None:
    target = write_starter_config(tmp_path / "nested" / "deeper" / "c.yaml")
    assert target.is_file()


def test_init_via_cli_writes_and_reports_ok(tmp_path: Path) -> None:
    target = tmp_path / "fresh.yaml"
    assert main(["--init", str(target)]) == 0
    assert target.is_file()


def test_init_via_cli_refuses_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "fresh.yaml"
    target.write_text("existing: true\n", encoding="utf-8")
    assert main(["--init", str(target)]) == 2


def test_init_via_cli_reports_unwritable_path(tmp_path: Path) -> None:
    """A directory where a file should go is an OSError, not a crash."""
    target = tmp_path / "adir"
    target.mkdir()
    assert main(["--init", str(target)]) == 2


# -- dry run ------------------------------------------------------------


def _dry_config(tmp_path: Path):
    target = write_starter_config(tmp_path / DEFAULT_CONFIG_NAME)
    config, _ = load_config_with_dir(str(target))
    return config


def test_stub_client_spends_nothing(tmp_path: Path) -> None:
    config = _dry_config(tmp_path)
    client = StubClient(config)
    assert STUB_PRICE_USD == 0.0
    assert client.running_cost_usd() == 0.0
    assert client.cost_by_model() == {}


def test_stub_client_answers_every_configured_agent(tmp_path: Path) -> None:
    config = _dry_config(tmp_path)
    client = StubClient(config)
    for key, agent in config.agents.items():
        reply = asyncio.run(client.call(agent, f"do {key}"))
        assert not reply.failed
        assert reply.text.strip(), f"stub returned nothing for {key}"
    assert client.call_count == len(config.agents)
    assert client.running_cost_usd() == 0.0


def test_stub_builder_output_passes_the_syntax_gate(tmp_path: Path) -> None:
    """A stub that emitted prose would make every dry run fail identically."""
    config = _dry_config(tmp_path)
    client = StubClient(config)
    reply = asyncio.run(client.call(config.agents["builder"], "build a cart"))
    adapter = adapter_for(DEFAULT_LANGUAGE)
    from looper.phases.workspace import strip_code_fences

    ok, note = adapter.parse_ok(strip_code_fences(reply.text))
    assert ok, note


def test_dry_run_build_is_free_and_honest(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: no key, no network, and a verdict that reflects the host."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    target = write_starter_config(tmp_path / DEFAULT_CONFIG_NAME)
    report_path = tmp_path / "report.json"
    code = main(
        [
            "--config",
            str(target),
            "--dry-run",
            "--goal",
            "build a shopping cart",
            "--report",
            str(report_path),
        ]
    )
    # Exit 0 or 3 both acceptable: on a host with no sandbox the test weight
    # is correctly withheld and the build is rejected. What must hold is that
    # the run completed and cost nothing.
    assert code in (0, 3)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["cost"]["usd"] == 0.0
    assert payload["verdict"]["exit_code"] == code


# -- report -------------------------------------------------------------


def _report(**overrides):
    base = dict(
        goal="build a cart",
        status="done",
        score=96.0,
        min_acceptable=95.0,
        target_score=99.0,
        cycles=2,
        exit_code=0,
        score_breakdown={"build": 20.0, "tests": 30.0, "caps_applied": []},
        cost_usd=0.1234,
        cost_by_model={"anthropic/claude-opus-4": 0.1234},
        token_usage={"total_tokens": 100},
        llm_calls=7,
        phases=[{"phase": "build", "status": "done", "summary": "ok"}],
        artifacts=["src/generated_code.py"],
        dry_run=False,
    )
    base.update(overrides)
    return build_report(**base)


def test_report_verdict_agrees_with_the_exit_code() -> None:
    accepted = _report(score=96.0, exit_code=0)
    assert accepted["verdict"]["accepted"] is True
    assert accepted["verdict"]["exit_meaning"].startswith("accepted")

    rejected = _report(score=40.0, exit_code=3)
    assert rejected["verdict"]["accepted"] is False
    assert "below min_acceptable" in rejected["verdict"]["exit_meaning"]


def test_report_explains_every_exit_code() -> None:
    for code in (0, 2, 3, 4, 5, 6, 130):
        meaning = _report(exit_code=code)["verdict"]["exit_meaning"]
        assert meaning and meaning != "unknown", code
    assert _report(exit_code=99)["verdict"]["exit_meaning"] == "unknown"


def test_report_is_written_as_valid_json(tmp_path: Path) -> None:
    destination = tmp_path / "out" / "run.json"
    write_run_report(_report(), destination)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    assert payload["cost"]["by_model"]["anthropic/claude-opus-4"] == 0.1234


def test_step_summary_is_appended_not_clobbered(tmp_path: Path) -> None:
    """GITHUB_STEP_SUMMARY is shared: overwriting it eats other jobs' output."""
    summary = tmp_path / "summary.md"
    summary.write_text("# Earlier step\n", encoding="utf-8")
    write_step_summary(_report(), summary)
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("# Earlier step")
    assert "## Looper: PASSED" in text
    assert "96.0" in text


def test_step_summary_marks_a_rejected_build(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    write_step_summary(_report(score=10.0, exit_code=3), summary)
    text = summary.read_text(encoding="utf-8")
    assert "REJECTED" in text


def test_step_summary_flags_a_dry_run(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    write_step_summary(_report(dry_run=True), summary)
    assert "dry run" in summary.read_text(encoding="utf-8").lower()


def test_report_truncates_a_runaway_phase_history() -> None:
    phases = [{"phase": f"p{i}", "status": "done", "summary": "s"} for i in range(500)]
    payload = _report(phases=phases)
    assert len(payload["phases"]) < 500


# -- languages ----------------------------------------------------------


def test_python_is_the_default_and_is_registered() -> None:
    assert DEFAULT_LANGUAGE == "python"
    assert "python" in supported_languages()
    assert isinstance(adapter_for("python"), PythonAdapter)


def test_unknown_language_is_refused_by_the_config_gate() -> None:
    with pytest.raises(ConfigError, match="execution.language"):
        build_config({"execution": {"language": "cobol"}})


def test_lint_mode_is_validated_against_the_language() -> None:
    with pytest.raises(ConfigError, match="lint_generated"):
        build_config({"execution": {"lint_generated": "eslint"}})


def test_lint_modes_come_from_the_adapter() -> None:
    assert lint_modes_for("python") == ("off", "py_compile", "flake8")


def test_python_adapter_argv_matches_the_pipeline_contract() -> None:
    adapter = PythonAdapter()
    assert adapter.lint_argv("x.py", "off") == []
    assert "py_compile" in adapter.lint_argv("x.py", "py_compile")
    assert "flake8" in adapter.lint_argv("x.py", "flake8")
    test_argv = adapter.test_argv("tests")
    assert "pytest" in test_argv
    # -I would imply -P and drop the script dir from sys.path, breaking every
    # suite that imports the module under test. Guard the fix.
    assert "-I" not in test_argv
    assert "-E" in test_argv and "-s" in test_argv


def test_python_adapter_parse_ok_rejects_empty_and_broken() -> None:
    adapter = PythonAdapter()
    ok, note = adapter.parse_ok("   \n")
    assert not ok and "empty" in note
    ok, note = adapter.parse_ok("def broken(:\n")
    assert not ok and "syntax error" in note
    ok, note = adapter.parse_ok("x = 1\n")
    assert ok and note == ""


# -- calibration metrics ------------------------------------------------


def test_metrics_count_the_four_quadrants() -> None:
    metrics = evaluate_gate([(True, True), (True, False), (False, True), (False, False)])
    assert (metrics.true_positives, metrics.false_negatives) == (1, 1)
    assert (metrics.false_positives, metrics.true_negatives) == (1, 1)
    assert metrics.total == 4
    assert metrics.precision == 0.5
    assert metrics.recall == 0.5
    assert metrics.false_positive_rate == 0.5
    assert metrics.f1 == 0.5


def test_metrics_are_vacuously_perfect_on_an_empty_corpus() -> None:
    metrics = evaluate_gate([])
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.false_positive_rate == 0.0
    assert metrics.f1 == 1.0


def test_f1_is_zero_when_both_terms_are_zero() -> None:
    metrics = GateMetrics(true_positives=0, false_positives=1, true_negatives=0, false_negatives=1)
    assert metrics.precision == 0.0
    assert metrics.recall == 0.0
    assert metrics.f1 == 0.0


def test_metrics_serialise_for_a_ci_log() -> None:
    payload = evaluate_gate([(True, True)]).as_dict()
    assert payload["recall"] == 1.0
    assert set(payload) >= {"precision", "recall", "false_positive_rate", "f1"}
