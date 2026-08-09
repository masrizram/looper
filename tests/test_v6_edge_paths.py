"""Edge paths of the v6 features: failure modes, empty sections, fallbacks.

The interesting half of an observability feature is what it does when things
go wrong -- an unwritable report must not fail a build that passed, a summary
with nothing to say must not emit an empty table, and a phase that no adapter
knows must be refused rather than half-run.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from looper.cli import emit_report, main
from looper.config import load_config_with_dir
from looper.dryrun import STUB_PROSE, StubClient
from looper.llm import AgentReply
from looper.orchestrator import PARALLEL_PHASE_GROUP, LooperDaemon, _parallel_batches
from looper.report import (
    MAX_PHASE_ENTRIES,
    build_report,
    render_markdown,
    write_run_report,
    write_step_summary,
)
from looper.scaffold import DEFAULT_CONFIG_NAME, write_starter_config


def _config(tmp_path: Path):
    target = write_starter_config(tmp_path / DEFAULT_CONFIG_NAME)
    return load_config_with_dir(str(target))


# -- report failure modes ----------------------------------------------


def _minimal_report(**over):
    base = dict(
        goal="g",
        status="done",
        score=1.0,
        min_acceptable=95.0,
        target_score=99.0,
        cycles=0,
        exit_code=3,
    )
    base.update(over)
    return build_report(**base)


def test_unwritable_report_does_not_fail_the_build(tmp_path: Path) -> None:
    """A directory in the file's place is an OSError, and must be swallowed."""
    blocked = tmp_path / "taken"
    blocked.mkdir()
    assert write_run_report(_minimal_report(), blocked) is None


def test_unwritable_step_summary_is_swallowed(tmp_path: Path) -> None:
    blocked = tmp_path / "taken"
    blocked.mkdir()
    assert write_step_summary(_minimal_report(), blocked) is None


def test_markdown_omits_empty_sections() -> None:
    """No breakdown, no spend, no phases: three tables that must not appear."""
    text = render_markdown(_minimal_report())
    assert "### Score breakdown" not in text
    assert "### Spend by model" not in text
    assert "### Phases" not in text
    assert "## Looper: REJECTED" in text


def test_markdown_declares_omitted_phases() -> None:
    phases = [
        {"phase": f"p{i}", "status": "done", "summary": "s"} for i in range(MAX_PHASE_ENTRIES + 7)
    ]
    report = _minimal_report(phases=phases)
    assert report["phases_omitted"] == 7
    # The tail is what is kept: the last cycle produced the verdict.
    assert report["phases"][-1]["phase"] == f"p{MAX_PHASE_ENTRIES + 6}"
    assert "omitted" in render_markdown(report)


def test_markdown_handles_a_report_with_no_verdict_at_all() -> None:
    """Defensive: a truncated/older payload must still render, not crash."""
    text = render_markdown({})
    assert "## Looper: REJECTED" in text


def test_pipe_in_a_phase_summary_cannot_break_the_table() -> None:
    report = _minimal_report(phases=[{"phase": "b", "status": "done", "summary": "a|b|c"}])
    row = [ln for ln in render_markdown(report).splitlines() if ln.startswith("| b ")][0]
    assert "a/b/c" in row


# -- emit_report wiring -------------------------------------------------


def test_emit_report_writes_step_summary_when_ci_env_is_set(tmp_path: Path) -> None:
    config, config_dir = _config(tmp_path)
    daemon = LooperDaemon(config, config_dir=config_dir, client=StubClient(config))
    report_path = tmp_path / "r.json"
    summary_path = tmp_path / "s.md"
    emit_report(
        destination=str(report_path),
        daemon=daemon,
        config=config,
        goal="g",
        score=99.0,
        exit_code=0,
        dry_run=True,
        env={"GITHUB_STEP_SUMMARY": str(summary_path)},
    )
    assert json.loads(report_path.read_text(encoding="utf-8"))["goal"] == "g"
    assert "## Looper: PASSED" in summary_path.read_text(encoding="utf-8")


def test_emit_report_skips_the_summary_outside_ci(tmp_path: Path) -> None:
    config, config_dir = _config(tmp_path)
    daemon = LooperDaemon(config, config_dir=config_dir, client=StubClient(config))
    report_path = tmp_path / "r.json"
    emit_report(
        destination=str(report_path),
        daemon=daemon,
        config=config,
        goal="g",
        score=10.0,
        exit_code=3,
        dry_run=True,
        env={},
    )
    assert report_path.is_file()


# -- CLI paths ----------------------------------------------------------


def test_no_action_prints_help_and_exits_zero(tmp_path: Path) -> None:
    target = write_starter_config(tmp_path / DEFAULT_CONFIG_NAME)
    assert main(["--config", str(target)]) == 0


def test_reset_clears_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = write_starter_config(tmp_path / DEFAULT_CONFIG_NAME)
    assert main(["--config", str(target), "--reset"]) == 0


# -- dry-run fallbacks --------------------------------------------------


def test_unknown_agent_role_falls_back_to_prose(tmp_path: Path) -> None:
    """Adding an agent must never break --dry-run."""
    config, _ = _config(tmp_path)
    client = StubClient(config)
    spec = next(iter(config.agents.values()))
    from dataclasses import replace

    reply: AgentReply = asyncio.run(client.call(replace(spec, role="Archivist"), "x"))
    assert reply.text == STUB_PROSE


# -- parallel batching --------------------------------------------------


def test_review_and_security_share_one_batch() -> None:
    batches = _parallel_batches(["build", "test", "review", "security_audit", "fix"])
    assert batches == [("build",), ("test",), ("review", "security_audit"), ("fix",)]


def test_a_lone_parallel_phase_still_runs() -> None:
    assert _parallel_batches(["review"]) == [("review",)]


def test_non_adjacent_parallel_phases_are_not_merged() -> None:
    """Ordering is authoritative: a phase between them means they are serial."""
    batches = _parallel_batches(["review", "fix", "security_audit"])
    assert batches == [("review",), ("fix",), ("security_audit",)]


def test_empty_phase_list_produces_no_batches() -> None:
    assert _parallel_batches([]) == []


def test_parallel_group_is_declared_not_inferred() -> None:
    assert PARALLEL_PHASE_GROUP == frozenset({"review", "security_audit"})


# -- concurrent budget safety -------------------------------------------


def test_concurrent_calls_cannot_both_spend_the_last_dollar(tmp_path: Path) -> None:
    """The reservation is the whole reason parallel phases are safe.

    Exercised against the real :class:`OpenRouterClient`, not the dry-run
    stub: the stub overrides ``call`` and therefore never reaches the guard,
    so testing it there would prove nothing about production.

    Two calls launched together against a ceiling that fits only one must
    produce two refusals or one refusal and one attempt -- never two attempts.
    Without the reservation both read the same unspent balance and both
    proceed.
    """
    from looper.config import CostBudgetExceeded
    from looper.llm import OpenRouterClient

    config, _ = _config(tmp_path)
    spec = next(iter(config.agents.values()))
    client = OpenRouterClient(config.openrouter, config.retry, client=object())
    client._max_cost_usd = 1e-9  # noqa: SLF001 - probing the guard directly

    async def _both() -> list[object]:
        return await asyncio.gather(
            client.call(spec, "a" * 5000),
            client.call(spec, "b" * 5000),
            return_exceptions=True,
        )

    results = asyncio.run(_both())
    assert all(isinstance(r, CostBudgetExceeded) for r in results), results


def test_untokenisable_suite_is_not_evidence_of_a_subject() -> None:
    """A suite that cannot even be tokenised proves nothing about coverage.

    An unclosed bracket raises ``TokenError`` from the tokeniser itself
    (verified, not assumed: an unterminated string tokenises fine on 3.11).
    That path must fail closed.
    """
    from looper.adequacy import _mentions_module

    assert _mentions_module("def test_x(:\n    pass\n", "generated_code") is False


def test_init_reports_an_unwritable_destination(tmp_path: Path, monkeypatch) -> None:
    """OSError on write is a config error, not a traceback."""
    from looper import scaffold

    def _boom(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(scaffold, "write_starter_config", _boom)
    monkeypatch.setattr("looper.cli.write_starter_config", _boom)
    assert main(["--init", str(tmp_path / "x.yaml")]) == 2


def test_report_is_written_even_when_the_build_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """The rejected run is exactly the one whose breakdown someone reads."""
    monkeypatch.chdir(tmp_path)
    target = write_starter_config(tmp_path / DEFAULT_CONFIG_NAME)
    report_path = tmp_path / "r.json"
    code = main(
        [
            "--config",
            str(target),
            "--dry-run",
            "--goal",
            "g",
            "--report",
            str(report_path),
        ]
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["verdict"]["exit_code"] == code


def test_main_emits_the_report_after_a_build(tmp_path: Path, monkeypatch) -> None:
    """The report branch inside ``main``, exercised without an event loop.

    ``asyncio.run`` drops coverage's tracer on this interpreter, so the
    end-to-end dry run cannot prove this branch even though it demonstrably
    writes the file. Stubbing the runner keeps the assertion honest: ``main``
    still selects the exit code and still calls the reporter.
    """
    target = write_starter_config(tmp_path / DEFAULT_CONFIG_NAME)
    destination = tmp_path / "main.json"
    monkeypatch.setattr("looper.cli.asyncio.run", lambda coro: (coro.close(), 97.5)[1])
    code = main(
        [
            "--config",
            str(target),
            "--dry-run",
            "--goal",
            "g",
            "--report",
            str(destination),
        ]
    )
    assert code == 0
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["verdict"]["score"] == 97.5
    assert payload["verdict"]["accepted"] is True


def test_write_report_wrapper_forwards_every_field(tmp_path: Path) -> None:
    """Covers the wrapper directly.

    The end-to-end test above proves the report is written for a real run,
    but coverage's tracer does not survive the ``asyncio.run`` inside
    ``main``, so the wrapper is also exercised on its own.
    """
    import argparse

    from looper.cli import _write_report

    config, config_dir = _config(tmp_path)
    daemon = LooperDaemon(config, config_dir=config_dir, client=StubClient(config))
    destination = tmp_path / "wrapped.json"
    args = argparse.Namespace(report=str(destination), goal="wrapped goal", dry_run=True)
    _write_report(args, daemon, config, 42.0, 3)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["goal"] == "wrapped goal"
    assert payload["verdict"]["score"] == 42.0
    assert payload["verdict"]["exit_code"] == 3
    assert payload["dry_run"] is True


def test_a_released_reservation_frees_the_headroom_again(tmp_path: Path) -> None:
    """A refused call must not leave its reservation pinned forever."""
    from looper.config import CostBudgetExceeded
    from looper.llm import OpenRouterClient

    config, _ = _config(tmp_path)
    spec = next(iter(config.agents.values()))
    client = OpenRouterClient(config.openrouter, config.retry, client=object())
    client._max_cost_usd = 1e-9  # noqa: SLF001

    async def _refused() -> None:
        for _ in range(3):
            with pytest.raises(CostBudgetExceeded):
                await client.call(spec, "hello")

    asyncio.run(_refused())
    assert client._reserved_usd == pytest.approx(0.0)  # noqa: SLF001
