"""pytest output parsing."""

from __future__ import annotations

import pytest

from looper.testparse import parse_test_summary


def test_counts_from_summary_line():
    out = (
        "tests/test_gen.py::test_foo PASSED [ 50%]\n"
        "tests/test_gen.py::test_bar PASSED [100%]\n"
        "2 passed in 0.01s"
    )
    assert parse_test_summary(out) == (2, 0)


def test_handles_failed():
    assert parse_test_summary("1 failed in 0.01s") == (0, 1)


def test_mixed_counts():
    assert parse_test_summary("3 passed, 2 failed in 0.02s") == (3, 2)


def test_no_double_count_for_test_named_passed():
    """Substring counting used to inflate this to 2 passes."""
    out = "tests/test_gen.py::test_passed_flag FAILED [100%]\n1 failed in 0.01s"
    assert parse_test_summary(out) == (0, 1)


def test_collection_errors_count_as_failures():
    assert parse_test_summary("2 errors in 0.1s") == (0, 2)


def test_singular_error_counts_as_failure():
    assert parse_test_summary("1 error in 0.1s") == (0, 1)


def test_xfailed_is_neither_pass_nor_fail():
    passed, failed = parse_test_summary("1 passed, 2 xfailed in 0.1s")
    assert (passed, failed) == (1, 0)


def test_xpassed_counts_as_failure():
    """An unexpected pass means the expectation is wrong, not that all is well."""
    assert parse_test_summary("1 xpassed in 0.1s") == (0, 1)


def test_no_tests_ran_is_zero_zero():
    assert parse_test_summary("no tests ran in 0.01s") == (0, 0)


def test_reads_stderr_too():
    assert parse_test_summary("", "3 passed in 1s") == (3, 0)


def test_empty_output_is_zero_zero():
    assert parse_test_summary("") == (0, 0)
    assert parse_test_summary("", "") == (0, 0)


def test_none_safe():
    assert parse_test_summary(None, None) == (0, 0)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("=== 10 passed, 1 failed, 2 errors in 3.4s ===", (10, 3)),
        ("5 failed, 5 passed", (5, 5)),
    ],
)
def test_realistic_summary_lines(text, expected):
    assert parse_test_summary(text) == expected
