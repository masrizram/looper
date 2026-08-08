"""Phase result and cycle-evidence types.

Kept separate from the phase logic so scoring, the orchestrator, and the
tests can depend on the data contract without importing subprocess handling,
filesystem sinks, or the LLM client.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


class WorkspaceEscapeError(ValueError):
    """Raised when a target path would resolve outside the workspace."""


@dataclass(frozen=True, slots=True)
class PhaseResult:
    """Structured outcome of a phase.

    Every risk-bearing field defaults to the *pessimistic* value. The previous
    dict-based contract used ``result.get("build_ok", True)``, so a phase that
    forgot the key was silently treated as a success.
    """

    phase: str
    agent: str
    model: str
    ok: bool = False
    summary: str = ""
    files_created: tuple[str, ...] = ()
    build_ok: bool = False
    tests_passed: int = 0
    tests_total: int = 0
    review_score: float = 0.0
    security_issues: tuple[str, ...] = ()
    error: str = ""
    #: True when the agent failed because the provider returned 402 (account
    #: out of credits). Lets the orchestrator abort the whole build instead of
    #: grinding through the remaining agents/cycles on a condition that cannot
    #: resolve on its own.
    out_of_credits: bool = False

    @property
    def status(self) -> str:
        return "done" if self.ok else "error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "status": self.status,
            "agent": self.agent,
            "model": self.model,
            "summary": self.summary,
            "files_created": list(self.files_created),
            "build_ok": self.build_ok,
            "tests_passed": self.tests_passed,
            "tests_total": self.tests_total,
            "review_score": self.review_score,
            "security_issues": list(self.security_issues),
            "error": self.error,
            "out_of_credits": self.out_of_credits,
        }


@dataclass
class CycleEvidence:
    """Accumulated facts for one cycle, fed to the scoring engine."""

    build_ok: bool = False
    tests_passed: int = 0
    tests_total: int = 0
    review_score: float = 0.0
    security_issues: list[str] = field(default_factory=list)

    def absorb(self, result: PhaseResult) -> None:
        if result.phase in ("build", "fix"):
            self.build_ok = result.build_ok
        elif result.phase == "test":
            self.tests_passed = result.tests_passed
            self.tests_total = result.tests_total
        elif result.phase == "review":
            self.review_score = result.review_score
        elif result.phase == "security_audit":
            self.security_issues = list(result.security_issues)

    def invalidate_unverified(self, phases: Sequence[str]) -> None:
        """Drop evidence no phase in this cycle will re-establish.

        Carrying a previous cycle's review score or empty findings list into a
        cycle that does not re-run those phases scores unverified facts as
        verified. A trimmed ``retry_phases`` could therefore keep banking a 98
        review from cycle 1 forever.
        """
        if "review" not in phases:
            self.review_score = 0.0
        if "security_audit" not in phases:
            self.security_issues = ["MEDIUM: security audit not re-run this cycle"]
        if "test" not in phases:
            self.tests_passed = 0
            self.tests_total = 0


def replace_result(result: PhaseResult, **changes: Any) -> PhaseResult:
    """``dataclasses.replace`` for :class:`PhaseResult` (frozen + slots)."""
    return dataclasses.replace(result, **changes)
