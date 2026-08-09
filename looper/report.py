"""Per-build run report: the machine-readable record of one build.

A build's verdict previously survived only as log lines and a mutable state
file. Neither answers the question a reviewer actually asks -- *why did this
build score what it scored, and what did it cost?* -- and neither can be
attached to a CI run, diffed between cycles, or archived.

:func:`build_report` produces that record as plain data, and
:func:`render_markdown` renders the same data for
``$GITHUB_STEP_SUMMARY``. Both are pure functions of their inputs: no
filesystem, no clock, no config lookups, so the numbers in the report are
exactly the numbers the run produced.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger("looper.report")

#: Default filename, written next to the state file.
REPORT_FILENAME = "looper_run_report.json"

#: Exit codes worth explaining in the summary. Mirrors ``looper.cli``; kept
#: as text here so the report is readable without the source at hand.
EXIT_MEANINGS: Mapping[int, str] = {
    0: "accepted (score >= min_acceptable)",
    2: "configuration error",
    3: "rejected: score below min_acceptable",
    4: "aborted: cost ceiling reached",
    5: "aborted: no sandbox backend available",
    6: "aborted: provider out of credits",
    130: "interrupted",
}


#: Cap on phase entries kept in the report. Five cycles of ten agents is
#: fifty entries; anything approaching this bound means a pathological run,
#: and a step summary with hundreds of rows is one nobody reads. The *tail*
#: is kept because the last cycle is the one that produced the verdict.
MAX_PHASE_ENTRIES = 120


def build_report(
    *,
    goal: str,
    status: str,
    score: float,
    min_acceptable: float,
    target_score: float,
    cycles: int,
    exit_code: int,
    score_breakdown: Mapping[str, Any] | None = None,
    cost_usd: float = 0.0,
    cost_by_model: Mapping[str, float] | None = None,
    token_usage: Mapping[str, int] | None = None,
    llm_calls: int = 0,
    phases: Sequence[Mapping[str, Any]] = (),
    artifacts: Sequence[str] = (),
    dry_run: bool = False,
) -> dict[str, Any]:
    """Assemble the report payload.

    ``accepted`` is derived from the score against ``min_acceptable`` rather
    than taken as an argument: a report that could disagree with the gate
    about whether the build passed would be worse than no report.
    """
    kept = list(phases)[-MAX_PHASE_ENTRIES:]
    return {
        "schema": 1,
        "goal": goal,
        "status": status,
        "dry_run": dry_run,
        "verdict": {
            "score": round(float(score), 2),
            "min_acceptable": float(min_acceptable),
            "target_score": float(target_score),
            "accepted": float(score) >= float(min_acceptable),
            "exit_code": int(exit_code),
            "exit_meaning": EXIT_MEANINGS.get(int(exit_code), "unknown"),
            "breakdown": dict(score_breakdown or {}),
        },
        "cost": {
            "usd": round(float(cost_usd), 6),
            "by_model": dict(cost_by_model or {}),
            "llm_calls": int(llm_calls),
            "tokens": dict(token_usage or {}),
        },
        "cycles": int(cycles),
        "phases": [dict(entry) for entry in kept],
        "phases_omitted": max(0, len(phases) - len(kept)),
        "artifacts": list(artifacts),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render ``report`` as the Markdown summary CI shows to a human."""
    verdict = report.get("verdict", {})
    cost = report.get("cost", {})
    accepted = bool(verdict.get("accepted"))
    headline = "PASSED" if accepted else "REJECTED"
    lines = [
        f"## Looper: {headline}",
        "",
        f"**Goal:** {report.get('goal', '')}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Score | {verdict.get('score', 0)} |",
        f"| Minimum accepted | {verdict.get('min_acceptable', 0)} |",
        f"| Target | {verdict.get('target_score', 0)} |",
        f"| Cycles run | {report.get('cycles', 0)} |",
        f"| Exit code | {verdict.get('exit_code', 0)} ({verdict.get('exit_meaning', '')}) |",
        f"| Spend | ${cost.get('usd', 0)} over {cost.get('llm_calls', 0)} call(s) |",
        f"| Dry run | {'yes' if report.get('dry_run') else 'no'} |",
    ]

    breakdown = verdict.get("breakdown") or {}
    if breakdown:
        lines += ["", "### Score breakdown", "", "| Component | Points |", "| --- | --- |"]
        lines += [f"| {name} | {value} |" for name, value in sorted(breakdown.items())]

    by_model = cost.get("by_model") or {}
    if by_model:
        lines += ["", "### Spend by model", "", "| Model | USD |", "| --- | --- |"]
        lines += [f"| {model} | {amount} |" for model, amount in sorted(by_model.items())]

    phases = report.get("phases") or []
    if phases:
        lines += ["", "### Phases", "", "| Phase | Status | Summary |", "| --- | --- | --- |"]
        for entry in phases:
            lines.append(
                f"| {entry.get('phase', '')} | {entry.get('status', '')} "
                f"| {str(entry.get('summary', '')).replace('|', '/')} |"
            )
        omitted = int(report.get("phases_omitted", 0) or 0)
        if omitted:
            # Silently showing the tail would misrepresent the run as shorter
            # than it was.
            lines += ["", f"_{omitted} earlier phase entr(y/ies) omitted._"]
    return "\n".join(lines) + "\n"


def write_run_report(report: Mapping[str, Any], path: Path) -> Path | None:
    """Write ``report`` as JSON to ``path``. Never raises.

    A report is an *observability* artifact. Failing a build that otherwise
    passed every gate because a disk was full or a directory was read-only
    would make the reporting feature a new way to lose work, so write errors
    are logged and swallowed -- the same rule the notifier follows.
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write run report to %s: %s", target, exc)
        return None
    logger.info("Run report written to %s", target)
    return target


def write_step_summary(report: Mapping[str, Any], path: Path) -> Path | None:
    """Append the Markdown summary to ``path`` (``$GITHUB_STEP_SUMMARY``)."""
    target = Path(path)
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(render_markdown(report))
    except OSError as exc:
        logger.warning("Could not write step summary to %s: %s", target, exc)
        return None
    return target
