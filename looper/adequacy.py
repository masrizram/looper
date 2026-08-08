"""Test-suite adequacy gate against self-test overfitting.

Because Looper asks the LLM to write BOTH the code and its tests, a weak
suite (a single ``assert True`` or a test that hardcodes the expected
verdict) would let the build score green without real verification. This
gate refuses such suites so a passing build means *something was actually
tested*.

See ADR-006 (verified evidence).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

#: Matches an assertion of the expected score, e.g. "assert score == 95"
#: or "assert total > 90". Hardcoding the gate value is the signature of a
#: test written to pass rather than to verify.
_SCORE_HARDCODE_RE = re.compile(
    r"assert\s+.*\b(score|total|build_ok|tests_passed)\b.*(==|>=|<=|>|<)",
    re.IGNORECASE,
)
_ASSERT_RE = re.compile(r"^\s*(async\s+)?def\s+test_", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class AdequacyReport:
    lines: int
    test_functions: int
    assertion_statements: int
    assertions_per_100_lines: float
    hardcodes_score: bool
    ok: bool
    reason: str


def _count_assert_statements(source: str) -> int:
    """Count ``assert`` statements that live inside ``def test_`` functions.

    A bare ``assert`` anywhere (e.g. inside a helper) does not count; only
    assertions that actually run as part of a test exercise the code.
    """
    if not source.strip():
        return 0
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    _attach_parents(tree)
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Assert) and _assert_in_test_function(node)
    )


def _assert_in_test_function(node: ast.Assert) -> bool:
    parent = getattr(node, "parent", None)
    while parent is not None:
        if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
            return parent.name.startswith("test_")
        parent = getattr(parent, "parent", None)
    return False


def _attach_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]


def evaluate_suite(source: str, *, min_assertions_per_100_lines: int) -> AdequacyReport:
    """Decide whether a generated test suite is strong enough to count."""
    lines = source.count("\n") + 1
    test_functions = len(_ASSERT_RE.findall(source))
    try:
        parsed = ast.parse(source)
    except SyntaxError:
        parsed = None
    if parsed is not None:
        _attach_parents(parsed)
    assertion_statements = _count_assert_statements(source)
    per_100 = round(assertion_statements / max(1, lines) * 100.0, 2)
    hardcodes = bool(_SCORE_HARDCODE_RE.search(source))

    if min_assertions_per_100_lines <= 0:
        return AdequacyReport(
            lines=lines,
            test_functions=test_functions,
            assertion_statements=assertion_statements,
            assertions_per_100_lines=per_100,
            hardcodes_score=hardcodes,
            ok=not hardcodes,
            reason="floor disabled" if not hardcodes else "hardcodes expected score",
        )

    if hardcodes:
        return AdequacyReport(
            lines=lines,
            test_functions=test_functions,
            assertion_statements=assertion_statements,
            assertions_per_100_lines=per_100,
            hardcodes_score=True,
            ok=False,
            reason="test hardcodes the expected score/verdict",
        )
    if test_functions == 0:
        return AdequacyReport(
            lines=lines,
            test_functions=0,
            assertion_statements=0,
            assertions_per_100_lines=0.0,
            hardcodes_score=False,
            ok=False,
            reason="no test functions found",
        )
    if per_100 < min_assertions_per_100_lines:
        return AdequacyReport(
            lines=lines,
            test_functions=test_functions,
            assertion_statements=assertion_statements,
            assertions_per_100_lines=per_100,
            hardcodes_score=False,
            ok=False,
            reason=(
                f"only {per_100} assertions/100 lines, need " f">= {min_assertions_per_100_lines}"
            ),
        )
    return AdequacyReport(
        lines=lines,
        test_functions=test_functions,
        assertion_statements=assertion_statements,
        assertions_per_100_lines=per_100,
        hardcodes_score=False,
        ok=True,
        reason="adequate",
    )
