"""Severity-weighted scoring, hard gates, and security-finding parsing."""

from __future__ import annotations

import pytest

from looper.config import ScoringWeights
from looper.scoring import (
    ScoringEngine,
    parse_security_findings,
    severity_of,
)


@pytest.fixture
def engine() -> ScoringEngine:
    return ScoringEngine(ScoringWeights())


# --- C-1: the regex that never matched --------------------------------------


def test_parses_plain_bullet_findings():
    """C-1 regression: the pattern contained a literal ``\\s`` inside a raw
    string, so it could never match and EVERY audit produced zero findings."""
    text = "- HIGH: SQL injection in login\n- MEDIUM: weak password policy\n"
    assert parse_security_findings(text) == [
        "HIGH: SQL injection in login",
        "MEDIUM: weak password policy",
    ]


@pytest.mark.parametrize(
    "line,expected",
    [
        ("- CRITICAL: rce", "CRITICAL: rce"),
        ("* HIGH: xss", "HIGH: xss"),
        ("+ LOW: typo", "LOW: typo"),
        ("- **CRITICAL** - hardcoded key", "CRITICAL: hardcoded key"),
        ("  - high : idor", "HIGH: idor"),
        ("- Medium missing csrf", "MEDIUM: missing csrf"),
        ("- LOW \u2013 verbose errors", "LOW: verbose errors"),
    ],
)
def test_parses_finding_format_variants(line, expected):
    assert parse_security_findings(line) == [expected]


def test_ignores_non_finding_prose():
    text = "This code is CRITICAL to the business.\nNo issues found."
    assert parse_security_findings(text) == []


def test_ignores_bullet_with_empty_description():
    assert parse_security_findings("- HIGH:") == []


def test_parse_handles_empty_input():
    assert parse_security_findings("") == []


def test_severity_of_unknown():
    assert severity_of("weird finding") == "UNKNOWN"
    assert severity_of("HIGH: x") == "HIGH"


# --- C-2: severity weighting ------------------------------------------------


def test_critical_costs_more_than_low(engine):
    """C-2 regression: the old flat len()*5 scored an RCE like a typo."""
    crit = engine.calculate_score(True, 10, 10, ["CRITICAL: rce"], 100)
    low = engine.calculate_score(True, 10, 10, ["LOW: typo"], 100)
    assert crit < low


def test_penalty_does_not_saturate(engine):
    """C-2: the old scorer floored at 6 findings, making 6 == 70."""
    six = engine.severity_penalty(["HIGH: x"] * 6)
    seventy = engine.severity_penalty(["CRITICAL: x"] * 70)
    assert seventy > six


def test_unknown_severity_uses_fallback_weight(engine):
    assert engine.severity_penalty(["something odd"]) == 5.0


# --- C-3: hard gates --------------------------------------------------------


def test_nothing_working_cannot_score_well(engine):
    """C-3 regression: no build + no tests used to score 30 because an empty
    security list handed out a free +30."""
    breakdown = engine.calculate(
        build_ok=False, tests_passed=0, tests_total=0, security_issues=[], review_score=0
    )
    assert breakdown.total <= 60.0
    assert "unverified_build" in breakdown.caps_applied


def test_perfect_review_cannot_rescue_a_failed_build(engine):
    breakdown = engine.calculate(
        build_ok=False, tests_passed=0, tests_total=0, security_issues=[], review_score=100
    )
    assert breakdown.total <= 60.0


def test_any_critical_finding_caps_below_release_band(engine):
    breakdown = engine.calculate(
        build_ok=True,
        tests_passed=10,
        tests_total=10,
        security_issues=["CRITICAL: rce"],
        review_score=100,
    )
    assert breakdown.total <= 50.0
    assert "critical_finding" in breakdown.caps_applied


def test_zero_tests_is_treated_as_unverified(engine):
    breakdown = engine.calculate(
        build_ok=True, tests_passed=0, tests_total=0, security_issues=[], review_score=100
    )
    assert breakdown.total <= 60.0


# --- Happy path & arithmetic ------------------------------------------------


def test_perfect_run_scores_100(engine):
    assert engine.calculate_score(True, 10, 10, [], 100) == 100.0


def test_breakdown_components(engine):
    breakdown = engine.calculate(
        build_ok=True,
        tests_passed=5,
        tests_total=10,
        security_issues=["HIGH: x", "LOW: y"],
        review_score=50,
    )
    assert breakdown.build == 20.0
    assert breakdown.tests == 15.0
    assert breakdown.security == 30.0 - 17.0
    assert breakdown.review == 10.0
    assert breakdown.total == pytest.approx(58.0)


def test_breakdown_serialises(engine):
    payload = engine.calculate(
        build_ok=True, tests_passed=1, tests_total=1, security_issues=[], review_score=80
    ).as_dict()
    assert set(payload) == {
        "build",
        "tests",
        "security",
        "review",
        "raw_total",
        "total",
        "caps_applied",
    }
    assert payload["caps_applied"] == []


def test_security_floor_is_zero_not_negative(engine):
    breakdown = engine.calculate(
        build_ok=True,
        tests_passed=1,
        tests_total=1,
        security_issues=["CRITICAL: a"] * 5,
        review_score=100,
    )
    assert breakdown.security == 0.0


def test_review_score_is_clamped(engine):
    high = engine.calculate(
        build_ok=True, tests_passed=1, tests_total=1, security_issues=[], review_score=1000
    )
    assert high.review == 20.0


def test_negative_review_score_floors_at_zero(engine):
    result = engine.calculate(
        build_ok=True, tests_passed=1, tests_total=1, security_issues=[], review_score=-50
    )
    assert result.review == 0.0


def test_rejects_impossible_test_counts(engine):
    with pytest.raises(ValueError, match="tests_passed"):
        engine.calculate(
            build_ok=True, tests_passed=5, tests_total=2, security_issues=[], review_score=0
        )


def test_rejects_negative_test_counts(engine):
    with pytest.raises(ValueError, match="non-negative"):
        engine.calculate(
            build_ok=True, tests_passed=-1, tests_total=-1, security_issues=[], review_score=0
        )


def test_custom_weights_change_the_result():
    engine = ScoringEngine(ScoringWeights(build=40, tests=20, security=20, review=20))
    breakdown = engine.calculate(
        build_ok=True, tests_passed=1, tests_total=1, security_issues=[], review_score=0
    )
    assert breakdown.build == 40.0


def test_engine_defaults_when_no_weights_given():
    assert ScoringEngine().weights.build == 20.0


def test_finding_with_only_decoration_is_dropped():
    """Covers the loop-continue branch: a match whose description cleans to
    the empty string must not become a phantom finding."""
    assert parse_security_findings("- HIGH: **") == []
    assert parse_security_findings("- HIGH: **\n- LOW: real issue") == ["LOW: real issue"]
