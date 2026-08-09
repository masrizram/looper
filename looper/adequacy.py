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
import io
import re
import token
import tokenize
from dataclasses import dataclass

#: Matches an assertion of a hardcoded *looper verdict*, e.g.
#: ``assert breakdown.raw_total == 95``. Scoped to identifiers this project
#: actually owns: an earlier version matched any name *containing* "total",
#: so a shopping-cart suite's ``assert c.total == 10`` was rejected as
#: "written to pass". Any domain with a ``total``/``score`` attribute --
#: invoices, quizzes, carts, games -- could therefore never clear the gate,
#: which is a false positive with the same cost as a miss (ADR-012).
_SCORE_HARDCODE_RE = re.compile(
    # Strong names: looper's own verdict fields, flagged with or without a
    # receiver -- `assert b.raw_total == 95` is a hardcoded verdict.
    r"assert\s+(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)*"
    r"\b(?:review_score|final_score|raw_total|score_total|build_ok|tests_passed|tests_total)\b"
    r"\s*(?:==|>=|<=|>|<|is)\s*(?:\d+(?:\.\d+)?|True|False)\s*(?:#[^\n]*)?$"
    # Weak names: bare `score` / `total` only. With a receiver they belong to
    # the domain under test (`cart.total`, `quiz.score`) and flagging those
    # made any such domain unable to clear the gate.
    r"|assert\s+\b(?:score|total)\b"
    r"\s*(?:==|>=|<=|>|<|is)\s*(?:\d+(?:\.\d+)?|True|False)\s*(?:#[^\n]*)?$",
    re.MULTILINE,
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


def _imports_subject(
    tree: ast.AST, subject_modules: frozenset[str] = frozenset(), source: str = ""
) -> bool:
    """True when the suite imports the artifact actually under test.

    A suite of ``assert 1 == 1`` is perfectly dense and tests nothing. The
    density floor measures effort, not coverage, so it happily passed
    tautologies while failing real suites.

    The first fix required "any import outside a stdlib denylist", which
    ``import logging`` satisfied -- so a tautological suite still passed. When
    the caller knows which modules the build produced, the check becomes
    exact: the suite must import *one of those*. When it does not (public API
    callers, a build that wrote no module), we fall back to the denylist so
    the gate degrades to its previous strictness instead of refusing
    everything.
    """
    imported = _imported_roots(tree)
    if subject_modules:
        # An exact import of the artifact is the strongest signal, but a suite
        # may legitimately reach the artifact without importing it by name --
        # reading ``src/generated_code.py`` from disk, or driving it as a
        # subprocess/CLI. Naming the module anywhere in the source is enough
        # to show the suite is connected to it; a tautology that names nothing
        # still fails.
        if imported & subject_modules:
            return True
        return any(_mentions_module(source, name) for name in subject_modules)
    return bool(imported - _NON_SUBJECT_MODULES)


def _mentions_module(source: str, module: str) -> bool:
    """True when ``module`` is *referenced*, as code or as an artifact path.

    A plain word-boundary search over the raw source counted a bare *string
    literal* or a *comment* as evidence, so a tautological suite passed the
    "is this connected to anything?" check by writing
    ``label = "generated_code"`` or ``# tests generated_code``. Both were
    accepted by the corpus probe (ADR-017).

    Two forms count, and only these two:

    * a NAME token -- the suite references the identifier in real code
      (``import generated_code``, ``generated_code.Cart()``);
    * a STRING token containing ``<module>.py`` -- the v5 case of a suite
      that reads or execs the artifact from disk rather than importing it.
      The ``.py`` suffix is what separates a path from a label: no
      tautological suite writes ``"generated_code.py"`` by accident.
    """
    filename = f"{module}.py"
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Untokenisable source is not evidence of a subject under test.
        return False
    for tok in tokens:
        if tok.type == token.NAME and tok.string == module:
            return True
        if tok.type == token.STRING and filename in tok.string:
            return True
    return False


def _imported_roots(tree: ast.AST) -> set[str]:
    """Top-level module names this source imports, by absolute syntax.

    Relative imports (``from . import sibling``) are deliberately excluded:
    the generated suite is written to a flat ``tests/`` directory that is not
    a package, so a relative import cannot resolve and is evidence of a
    confused suite rather than of a real subject under test.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
    return roots


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


def evaluate_suite(
    source: str,
    *,
    min_assertions_per_100_lines: int,
    subject_modules: frozenset[str] = frozenset(),
) -> AdequacyReport:
    """Decide whether a generated test suite is strong enough to count.

    ``subject_modules`` are the top-level module names the build actually
    produced. Supplying them turns the "is this suite connected to anything?"
    check from a stdlib denylist into an exact match, which is what stops a
    tautological suite passing on the strength of ``import logging``.
    """
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
    imports_subject = (
        _imports_subject(parsed, subject_modules, source) if parsed is not None else False
    )

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
