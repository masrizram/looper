"""Robust parsing of pytest CLI output."""

from __future__ import annotations

import re

#: pytest summary fragments: "3 passed", "2 failed", "1 error", "4 xfailed".
_SUMMARY_RE = re.compile(
    r"(\d+)\s+(passed|failed|errors?|xfailed|xpassed)\b",
    re.IGNORECASE,
)

#: The dedicated "no tests ran" line pytest emits with exit code 5.
_NO_TESTS_RE = re.compile(r"\bno tests ran\b", re.IGNORECASE)


def parse_test_summary(stdout: str, stderr: str = "") -> tuple[int, int]:
    """Return ``(passed, failed)`` from pytest output.

    Reads the summary line rather than counting per-line ``PASSED``/``FAILED``
    markers: substring counting double-counted results and mis-handled tests
    whose *names* contain the word "passed".

    Collection errors and ``xpassed`` count as failures - an unexpected pass
    means the test's own expectation is wrong, which is not evidence of health.
    """
    text = f"{stdout or ''}\n{stderr or ''}"

    passed = 0
    failed = 0
    matched = False
    for count_text, label in _SUMMARY_RE.findall(text):
        matched = True
        count = int(count_text)
        normalized = (
            label.lower().rstrip("s") if label.lower().startswith("error") else label.lower()
        )
        if normalized == "passed":
            passed += count
        elif normalized in {"failed", "error", "xpassed"}:
            failed += count
        # xfailed is an expected failure: neither a pass nor a regression.

    if not matched and _NO_TESTS_RE.search(text):
        return 0, 0

    return passed, failed
