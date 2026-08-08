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

#: Matches an assertion of a hardcoded expected verdict, e.g.
#: ``assert score == 95``. Requires a *literal* on the right-hand side: an
#: earlier version also matched ``assert result.tests_passed == expected``,
#: flagging legitimate suites (including looper's own) as written-to-pass.
_SCORE_HARDCODE_RE = re.compile(
    r"assert\s+[^\n=<>!]*\b(score|total|build_ok|tests_passed)\b[^\n]*"
    r"(==|>=|<=|>|<)\s*(\d+(?:\.\d+)?|True|False)\s*(?:#[^\n]*)?$",
    re.IGNORECASE | re.MULTILINE,
)
_ASSERT_RE = re.compile(r"^\s*(async\s+)?def\s+test_", re.MULTILINE)

#: ``unittest``-style assertions. A suite built on ``unittest.TestCase`` uses
#: ``self.assertEqual(...)`` and contains not one ``assert`` statement, so an
#: AST count of :class:`ast.Assert` alone rated a thorough suite at 0.0
#: assertions/100 lines and rejected it outright.
_UNITTEST_ASSERT_PREFIX = "assert"

#: Context managers that assert a behaviour rather than a value. A test whose
#: whole point is ``with pytest.raises(ValueError):`` carries no ``assert``
#: statement but is a real check, and was previously counted as nothing.
_RAISES_ATTRS: frozenset[str] = frozenset({"raises", "warns", "deprecated_call"})


@dataclass(frozen=True, slots=True)
class AdequacyReport:
    lines: int
    test_functions: int
    assertion_statements: int
    assertions_per_100_lines: float
    hardcodes_score: bool
    ok: bool
    reason: str
    imports_subject: bool = True


def _enclosing_test(node: ast.AST) -> bool:
    """True when ``node`` sits inside a ``def test_*`` function."""
    parent = getattr(node, "parent", None)
    while parent is not None:
        if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
            return parent.name.startswith("test_")
        parent = getattr(parent, "parent", None)
    return False


def _is_unittest_assert(node: ast.Call) -> bool:
    """``self.assertEqual(...)`` / ``self.assertRaises(...)`` and friends."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr.startswith(_UNITTEST_ASSERT_PREFIX)
        and len(func.attr) > len(_UNITTEST_ASSERT_PREFIX)
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    )


def _is_raises_context(node: ast.Call) -> bool:
    """``pytest.raises(...)`` / ``pytest.warns(...)`` used as an assertion."""
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in _RAISES_ATTRS


def _count_assert_statements(source: str, tree: ast.AST | None = None) -> int:
    """Count assertions inside ``def test_`` functions, across frameworks.

    Counting only :class:`ast.Assert` measured one dialect of one framework.
    A ``unittest.TestCase`` suite scored 0.0 assertions/100 lines and was
    rejected outright, and a test whose entire purpose was
    ``with pytest.raises(ValueError):`` contributed nothing. The gate exists
    to reject weak suites; rejecting strong ones is the same defect wearing
    the opposite sign.
    """
    if tree is None:
        if not source.strip():
            return 0
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return 0
        _attach_parents(tree)

    total = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and _enclosing_test(node):
            total += 1
        elif isinstance(node, ast.Call) and _enclosing_test(node):
            if _is_unittest_assert(node) or _is_raises_context(node):
                total += 1
    return total


def _counts_unittest_methods(tree: ast.AST) -> int:
    """Number of ``def test_*`` methods inside ``unittest.TestCase`` classes.

    ``_ASSERT_RE`` already matches these by name, so this exists only to make
    the intent explicit for suites that indent their tests inside a class.
    """
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    )


def _imports_subject(tree: ast.AST) -> bool:
    """True when the suite imports anything at all from outside itself.

    A suite of ``assert 1 == 1`` is perfectly dense and tests nothing. The
    density floor measures effort, not coverage, so it happily passed
    tautologies while failing real suites. Requiring at least one import of
    non-stdlib, non-test code is a cheap proxy for "this suite is connected
    to the artifact under test".
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module and module.split(".")[0] not in _NON_SUBJECT_MODULES:
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _NON_SUBJECT_MODULES:
                    return True
    return False


#: Imports that do not indicate a subject under test: the test framework
#: itself and the handful of stdlib helpers every suite pulls in.
_NON_SUBJECT_MODULES: frozenset[str] = frozenset(
    {
        "pytest",
        "unittest",
        "mock",
        "sys",
        "os",
        "re",
        "json",
        "math",
        "time",
        "typing",
        "pathlib",
        "dataclasses",
        "collections",
        "itertools",
        "functools",
        "datetime",
        "decimal",
        "random",
        "string",
        "io",
        "tempfile",
        "textwrap",
        "asyncio",
        "contextlib",
        "__future__",
    }
)


def _attach_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]


def evaluate_suite(source: str, *, min_assertions_per_100_lines: int) -> AdequacyReport:
    """Decide whether a generated test suite is strong enough to count."""
    lines = source.count("\n") + 1
    try:
        parsed = ast.parse(source)
    except SyntaxError:
        parsed = None
    if parsed is not None:
        _attach_parents(parsed)
    test_functions = (
        _counts_unittest_methods(parsed) if parsed is not None else len(_ASSERT_RE.findall(source))
    )
    # Reuse the tree we already built: parsing untrusted source twice was
    # pure waste, and the second parse silently discarded this one's result.
    assertion_statements = _count_assert_statements(source, parsed)
    per_100 = round(assertion_statements / max(1, lines) * 100.0, 2)
    hardcodes = bool(_SCORE_HARDCODE_RE.search(source))
    imports_subject = _imports_subject(parsed) if parsed is not None else False

    def _report(*, ok: bool, reason: str) -> AdequacyReport:
        return AdequacyReport(
            lines=lines,
            test_functions=test_functions,
            assertion_statements=assertion_statements,
            assertions_per_100_lines=per_100,
            hardcodes_score=hardcodes,
            ok=ok,
            reason=reason,
            imports_subject=imports_subject,
        )

    if hardcodes:
        return _report(ok=False, reason="test hardcodes the expected score/verdict")

    if min_assertions_per_100_lines <= 0:
        return _report(ok=True, reason="floor disabled")

    if test_functions == 0:
        return _report(ok=False, reason="no test functions found")
    if not imports_subject:
        return _report(
            ok=False,
            reason="suite imports nothing under test (tautological assertions only)",
        )
    if per_100 < min_assertions_per_100_lines:
        return _report(
            ok=False,
            reason=(
                f"only {per_100} assertions/100 lines, need " f">= {min_assertions_per_100_lines}"
            ),
        )
    return _report(ok=True, reason="adequate")
