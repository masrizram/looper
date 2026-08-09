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

#: Phrases an auditor uses to say "clean". Matched as a regex against the
#: lowered report, because a literal-substring list of three English phrases
#: reported "No security issues were identified" as a malformed report and
#: injected a phantom MEDIUM finding -- the exact false-positive class this
#: project refuses to ship.
NO_ISSUES_RE = re.compile(
    r"\bno\s+(?:known\s+|significant\s+|apparent\s+|obvious\s+)?"
    r"(?:security\s+)?(?:issues?|vulnerabilit(?:y|ies)|findings?|concerns?|"
    r"problems?|weaknesses)\b"
    r"|\bnothing\s+(?:of\s+concern|to\s+report)\b"
    r"|\b(?:issues?|vulnerabilit(?:y|ies)|findings?)\s*:\s*none\b"
    r"|\bnone\s+(?:found|identified|detected|observed)\b",
    re.IGNORECASE,
)

#: Words that flip the meaning of a "clean" phrase. "It is NOT true that there
#: are no vulnerabilities" matched the clean pattern and scored a report full
#: of findings as spotless -- a false negative in the one direction this
#: project cannot afford. When a negation precedes the match on the same line,
#: the phrase is not a clean bill of health.
_NEGATION_RE = re.compile(
    r"\b(?:not|isn't|is\s+not|aren't|are\s+not|wasn't|no\s+longer|false|untrue|"
    r"cannot\s+say|can't\s+say|hardly|barely|far\s+from)\b",
    re.IGNORECASE,
)

#: Kept for backwards compatibility with callers that iterate the markers.
NO_ISSUES_MARKERS = ("no issues found", "no vulnerabilities found", "no findings")


def reports_no_issues(text: str) -> bool:
    """True when an auditor's prose declares the code clean.

    Two ways prose lies about being clean, and both are refused:

    * a negation *anywhere* on the line -- the earlier version only looked at
      the text before the match, so "Are there no vulnerabilities? Absolutely
      not, I found 4 criticals." read as a clean bill of health;
    * a question -- "no issues?" is asking, not asserting.

    Erring toward "not clean" is the correct direction: a false clean scores a
    vulnerable build as spotless, while a false dirty only injects one MEDIUM
    "audit output not in expected format" finding the operator can see.
    """
    for line in (text or "").splitlines():
        match = NO_ISSUES_RE.search(line)
        if match is None:
            continue
        if _NEGATION_RE.search(line):
            continue
        if line.rstrip().endswith("?"):
            continue
        return True
    return False


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

    def summary_line(self) -> str:
        """One-line human summary, used as a git commit body (ADR-010)."""
        parts = (
            f"build={self.build:.0f} tests={self.tests:.0f} "
            f"security={self.security:.0f} review={self.review:.0f}"
        )
        if self.caps_applied:
            parts += f" caps={','.join(self.caps_applied)}"
        return parts


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
        # Volume is evidence too. Severity penalties alone let 50 UNKNOWN-
        # severity findings zero the security weight and still clear the gate
        # on build+tests+review, because no CRITICAL was present to trip a cap.
        # A report that long is not a passing build regardless of labelling.
        if len(security_issues) >= weights.findings_volume_threshold:
            caps.append("finding_volume")
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
