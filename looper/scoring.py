"""Severity-weighted scoring with hard release gates."""

from __future__ import annotations

import re
from dataclasses import dataclass

from looper.config import ScoringWeights

#: Matches "- HIGH: desc", "* **CRITICAL** - desc", "- low : desc".
#: The original pattern contained a literal ``\\s`` inside a raw string, so it
#: could never match and every security report silently produced zero findings.
SECURITY_FINDING_RE = re.compile(
    r"^[ \t]*[-*+]\s*\**\s*(CRITICAL|HIGH|MEDIUM|LOW)\**\s*[:\-\u2013]?\s*"
    r"(?=[^\s:\-\u2013])(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

NO_ISSUES_MARKERS = ("no issues found", "no vulnerabilities found", "no findings")

SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def parse_security_findings(text: str) -> list[str]:
    """Extract ``SEVERITY: description`` findings from an auditor's markdown."""
    findings: list[str] = []
    for severity, description in SECURITY_FINDING_RE.findall(text or ""):
        cleaned = description.strip().strip("*").strip()
        if cleaned:
            findings.append(f"{severity.upper()}: {cleaned}")
    return findings


def severity_of(issue: str) -> str:
    head = str(issue).split(":", 1)[0].strip().upper()
    return head if head in SEVERITY_ORDER else "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Why a score is what it is - surfaced in state and over HTTP."""

    build: float
    tests: float
    security: float
    review: float
    raw_total: float
    total: float
    caps_applied: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "build": round(self.build, 2),
            "tests": round(self.tests, 2),
            "security": round(self.security, 2),
            "review": round(self.review, 2),
            "raw_total": round(self.raw_total, 2),
            "total": round(self.total, 2),
            "caps_applied": list(self.caps_applied),
        }


class ScoringEngine:
    """Turns phase evidence into a 0-100 release score.

    Two properties the previous flat scorer lacked:

    * **Severity weighting** - one CRITICAL costs far more than one LOW, and
      the penalty does not saturate after six findings.
    * **Hard gates** - an unverified build or any CRITICAL finding caps the
      score below the release band, so no amount of review praise can push a
      broken build over the line.
    """

    def __init__(self, weights: ScoringWeights | None = None) -> None:
        self.weights = weights or ScoringWeights()

    def severity_penalty(self, security_issues: list[str]) -> float:
        return sum(self.weights.penalty_for(severity_of(issue)) for issue in security_issues)

    def calculate(
        self,
        *,
        build_ok: bool,
        tests_passed: int,
        tests_total: int,
        security_issues: list[str],
        review_score: float,
    ) -> ScoreBreakdown:
        weights = self.weights

        if tests_total < 0 or tests_passed < 0:
            raise ValueError("test counts must be non-negative")
        if tests_passed > tests_total:
            raise ValueError(f"tests_passed ({tests_passed}) > tests_total ({tests_total})")

        build_points = weights.build if build_ok else 0.0

        tests_points = 0.0
        if tests_total > 0:
            tests_points = (tests_passed / tests_total) * weights.tests

        security_points = max(0.0, weights.security - self.severity_penalty(security_issues))

        review_points = max(0.0, min(weights.review, (review_score / 100.0) * weights.review))

        raw_total = build_points + tests_points + security_points + review_points
        total = min(100.0, raw_total)

        caps: list[str] = []
        if not build_ok or tests_total == 0:
            caps.append("unverified_build")
            total = min(total, weights.unverified_build_cap)
        if any(severity_of(issue) == "CRITICAL" for issue in security_issues):
            caps.append("critical_finding")
            total = min(total, weights.critical_finding_cap)

        return ScoreBreakdown(
            build=build_points,
            tests=tests_points,
            security=security_points,
            review=review_points,
            raw_total=raw_total,
            total=total,
            caps_applied=tuple(caps),
        )

    def calculate_score(
        self,
        build_ok: bool,
        tests_passed: int,
        tests_total: int,
        security_issues: list[str],
        review_score: float,
    ) -> float:
        """Backwards-compatible positional wrapper returning just the number."""
        return self.calculate(
            build_ok=build_ok,
            tests_passed=tests_passed,
            tests_total=tests_total,
            security_issues=security_issues,
            review_score=review_score,
        ).total
