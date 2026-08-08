"""Proof tests for the sandbox scanner and test-suite adequacy gate.

These encode the threat model from the audit: LLM-authored code must never be
allowed to run destructively on the host, and a test suite that is one
``assert True`` must not let a build go green.
"""

from __future__ import annotations

import pytest

from looper.adequacy import evaluate_suite
from looper.sandbox import scan_for_dangerous_calls

DESTRUCTIVE_SAMPLES = [
    ("os.system('rm -rf /')", "shell execution via os.system"),
    ("import os; os.remove('x')", "filesystem deletion via os.remove"),
    ("subprocess.run(['ls'])", "child process spawn via subprocess"),
    ("import socket; s = socket.socket()", "raw socket / network access"),
    ("requests.get('http://evil')", "outbound HTTP via requests"),
    ("eval('1+1')", "dynamic execution via eval"),
    ("__import__('os')", "dynamic import via __import__"),
]


@pytest.mark.parametrize("code,reason", DESTRUCTIVE_SAMPLES)
def test_scan_flags_destructive_calls(code: str, reason: str):
    found = scan_for_dangerous_calls(code)
    assert any(reason in r for r in found), found


def test_scan_ignores_comment_mentions():
    """Mentioning a dangerous call in prose must not trip the guard."""
    assert scan_for_dangerous_calls("# do not use os.system here") == []


def test_scan_clean_code_passes():
    assert scan_for_dangerous_calls("def add(a, b):\n    return a + b\n") == []


def test_scan_call_with_lambda_func_is_safe():
    # A call whose func is a lambda (neither Name nor Attribute) must not be
    # flagged; exercises the elif fall-through arc (87->82).
    assert scan_for_dangerous_calls("x = (lambda: 0)()\n") == []


def test_scan_requires_actual_call_not_import():
    """Importing subprocess is fine; only calling it is dangerous."""
    assert scan_for_dangerous_calls("import subprocess\n") == []


def test_adequacy_rejects_single_assert():
    # A long file with only one assertion is too thin (below 6/100 lines).
    src = (
        "import thing\n\n\ndef test_x():\n    assert True\n"
        + "\n".join(f"# padding line {i}" for i in range(40))
        + "\n"
    )
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.ok is False
    assert "assertions/100 lines" in report.reason


def test_adequacy_rejects_hardcoded_score():
    src = "def test_gate():\n    assert score == 95\n    assert build_ok is True\n"
    report = evaluate_suite(src, min_assertions_per_100_lines=0)
    assert report.ok is False
    assert report.hardcodes_score is True


def test_adequacy_accepts_dense_suite():
    src = (
        "def test_a():\n"
        "    assert 1 + 1 == 2\n"
        "    assert 2 * 2 == 4\n"
        "    assert 'x' in 'xyz'\n"
        "    assert [1] == [1]\n"
        "    assert max([1, 2]) == 2\n"
        "    assert min([3, 1]) == 1\n"
        "    assert bool(0) is False\n"
        "    assert bool(1) is True\n"
    )
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.ok is True, report.reason


def test_adequacy_floor_disabled_skips_density():
    src = "def test_x():\n    assert True\n"
    report = evaluate_suite(src, min_assertions_per_100_lines=0)
    # Hardcode check still applies even when floor is off.
    assert report.ok is True


def test_adequacy_floor_disabled_but_hardcode_still_rejected():
    src = "def test_gate():\n    assert score == 95\n"
    report = evaluate_suite(src, min_assertions_per_100_lines=0)
    assert report.ok is False
    assert report.hardcodes_score is True


def test_adequacy_empty_source_is_zero():
    # Empty source -> 0 assertions, no test functions -> refused.
    report = evaluate_suite("   \n  \n", min_assertions_per_100_lines=6)
    assert report.assertion_statements == 0
    assert report.ok is False


def test_adequacy_syntax_error_source_counts_zero():
    # A malformed suite cannot be parsed -> counted as 0 assertions, not a crash.
    src = "def test_x(:\n    assert True\n"
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.assertion_statements == 0


def test_adequacy_floor_disabled_and_hardcode_is_rejected():
    # Floor off but the suite still hardcodes the verdict -> refused.
    src = "def test_v():\n    assert total >= 90\n"
    report = evaluate_suite(src, min_assertions_per_100_lines=0)
    assert report.ok is False
    assert report.hardcodes_score is True


def test_adequacy_no_test_functions_is_rejected():
    # Floor on, dense asserts but zero test_ functions -> not a real suite.
    src = "\n".join(f"    assert {i} == {i}" for i in range(20)) + "\n"
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.ok is False
    assert report.test_functions == 0
    assert "no test functions" in report.reason


def test_adequacy_assert_inside_nested_block_in_test_counts():
    # Assert nested inside an `if` inside a test_ fn must still count (parent
    # chain walks through non-FunctionDef nodes -> coverage of 64-65).
    src = (
        "def test_x():\n"
        "    for i in range(3):\n"
        "        if i == 1:\n"
        "            assert i == 1\n"
    )
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.assertion_statements == 1
    assert report.ok is True


def test_adequacy_top_level_assert_is_not_counted():
    # An assert at module scope (no enclosing test_ fn) must not count; walking
    # the parent chain hits `parent is None` -> returns False (line 65).
    src = "assert True\n\ndef test_x():\n    assert True\n"
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.assertion_statements == 1
    assert report.test_functions == 1


def test_scan_syntax_error_source_still_scans_substrings():
    # Malformed source must not raise; substring scan still runs (79-80).
    report = scan_for_dangerous_calls("def x(:\n    os.system('pwn')\n")
    assert any("os.system" in r for r in report)


def test_adequacy_hardcode_rejected_with_floor_on():
    # Floor > 0 and a score-hardcoding suite -> hardcode branch (line 100).
    src = "def test_gate():\n    assert score == 95\n"
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.ok is False
    assert report.hardcodes_score is True
