"""Regressions for the v5 audit.

Every test here failed before its fix and passes after. The v5 audit's point
was that a 100%-branch-covered suite still missed all of these: coverage
proves a line *ran*, not that it was *right*. So each test asserts the
behaviour a probe measured, not the shape of the code.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from looper.adequacy import evaluate_suite
from looper.config import AgentSpec, CostBudgetExceeded, OpenRouterConfig, RetryPolicy
from looper.llm import OpenRouterClient
from looper.phases.agents import REVIEW_SCORE_RE
from looper.sandbox import docker_argv, scan_for_dangerous_calls
from looper.scoring import reports_no_issues

# --------------------------------------------------------------------------
# C-1: the cost ceiling must be a ceiling.
# --------------------------------------------------------------------------


class _FakeCompletions:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls = 0

    async def create(self, **_: Any) -> Any:
        self.calls += 1
        outer = self

        class _Usage:
            prompt_tokens = outer.prompt_tokens
            completion_tokens = outer.completion_tokens

        class _Message:
            content = "ok"

        class _Choice:
            message = _Message()

        class _Response:
            usage = _Usage()
            choices = [_Choice()]

        return _Response()


def _client(budget: float, completions: _FakeCompletions) -> OpenRouterClient:
    class _Chat:
        def __init__(self) -> None:
            self.completions = completions

    class _Raw:
        def __init__(self) -> None:
            self.chat = _Chat()

    return OpenRouterClient(
        OpenRouterConfig(api_key="k"),
        RetryPolicy(max_attempts=1),
        client=_Raw(),
        model_prices_usd_per_1k={"anthropic/claude-opus-5": 0.015},
        max_cost_usd=budget,
    )


@pytest.mark.parametrize("budget", [0.5, 1.0, 5.0])
def test_c1_budget_is_never_exceeded(budget: float) -> None:
    """Spend stops at or below the ceiling, never one full call past it.

    Before: ``_check_budget`` ran *before* the request but cost was added
    *after* it, so the ceiling could only ever be noticed once already
    breached. A $1.00 budget bought a $15.00 Opus call.
    """
    completions = _FakeCompletions(prompt_tokens=60_000, completion_tokens=8_192)
    client = _client(budget, completions)
    spec = AgentSpec(role="builder", model="anthropic/claude-opus-5", max_tokens=8_192)

    async def _burn() -> None:
        for _ in range(50):
            await client.call(spec, "x" * 200_000)

    with pytest.raises(CostBudgetExceeded):
        asyncio.run(_burn())

    assert client.running_cost_usd() <= budget


def test_c1_refusal_happens_before_the_request_is_sent() -> None:
    """The expensive call is never issued, not merely accounted for."""
    completions = _FakeCompletions(prompt_tokens=60_000, completion_tokens=8_192)
    client = _client(0.01, completions)
    spec = AgentSpec(role="builder", model="anthropic/claude-opus-5", max_tokens=8_192)

    with pytest.raises(CostBudgetExceeded):
        asyncio.run(client.call(spec, "x" * 200_000))

    assert completions.calls == 0
    assert client.running_cost_usd() == 0.0


def test_c1_an_affordable_call_still_goes_through() -> None:
    """The projection must not refuse work that fits: fail-closed, not shut."""
    completions = _FakeCompletions(prompt_tokens=100, completion_tokens=50)
    client = _client(1000.0, completions)
    reply = asyncio.run(client.call(AgentSpec(role="b", model="anthropic/claude-opus-5"), "hi"))
    assert reply.ok is True
    assert completions.calls == 1


# --------------------------------------------------------------------------
# C-2: the adequacy gate was wrong in both directions.
# --------------------------------------------------------------------------

_LEGIT_CART_SUITE = """
from cart import Cart

def test_total_sums_items():
    c = Cart()
    c.add("apple", 3)
    c.add("pear", 7)
    assert c.total == 10
"""


def test_c2_a_domain_with_a_total_attribute_is_not_rejected() -> None:
    """``assert c.total == 10`` is a cart test, not a hardcoded verdict.

    Before: the pattern matched any identifier *containing* "total"/"score",
    so carts, invoices and quizzes could never clear ``target_score``.
    """
    report = evaluate_suite(
        _LEGIT_CART_SUITE,
        min_assertions_per_100_lines=3,
        subject_modules=frozenset({"cart"}),
    )
    assert report.hardcodes_score is False
    assert report.ok is True


@pytest.mark.parametrize(
    "line",
    [
        "assert b.raw_total == 95",
        "assert result.build_ok is True",
        "assert report.review_score == 88",
        "assert tests_passed == 5",
        "assert score == 95",
    ],
)
def test_c2_looper_verdict_hardcodes_are_still_flagged(line: str) -> None:
    assert REVIEW_SCORE_RE is not None  # module imported for the sibling test
    from looper.adequacy import _SCORE_HARDCODE_RE

    assert _SCORE_HARDCODE_RE.search(line) is not None


@pytest.mark.parametrize(
    "line",
    [
        "assert cart.subtotal == 10",
        "assert invoice.total >= 99.5",
        "assert q.score == 100",
        "assert result.tests_passed == expected",
    ],
)
def test_c2_domain_assertions_are_not_flagged(line: str) -> None:
    from looper.adequacy import _SCORE_HARDCODE_RE

    assert _SCORE_HARDCODE_RE.search(line) is None


# --------------------------------------------------------------------------
# C-3: a tautological suite passed by importing the standard library.
# --------------------------------------------------------------------------


def test_c3_tautology_cannot_pass_by_importing_logging() -> None:
    """``import logging`` is not evidence of testing anything.

    Before: the check was "imports anything outside a stdlib denylist", and
    ``logging`` was not on it, so ``assert 1 == 1`` scored as an adequate
    suite.
    """
    src = "import logging\n\ndef test_a():\n    assert 1 == 1\n\ndef test_b():\n    assert 2 == 2\n"
    report = evaluate_suite(
        src, min_assertions_per_100_lines=3, subject_modules=frozenset({"cart"})
    )
    assert report.ok is False
    assert "imports nothing under test" in report.reason


def test_c3_a_suite_that_reads_the_artifact_from_disk_is_accepted() -> None:
    """Not every real suite imports the module by name."""
    src = (
        "from pathlib import Path\n\n"
        "def test_generated_runs():\n"
        "    src = Path('src/generated_code.py').read_text()\n"
        "    assert 'print' in src\n"
    )
    report = evaluate_suite(
        src,
        min_assertions_per_100_lines=3,
        subject_modules=frozenset({"generated_code"}),
    )
    assert report.imports_subject is True


# --------------------------------------------------------------------------
# H-1: the sandbox tripwire only saw one spelling of a dangerous call.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ('import os as o\ndef test():\n    o.system("rm -rf /")\n', "aliased import"),
        ('from os import system\ndef test():\n    system("ls")\n', "from-import"),
        ("import subprocess as sp\ndef test():\n    sp.run(['ls'])\n", "aliased subprocess"),
        ('from shutil import rmtree\ndef test():\n    rmtree("/")\n', "from-imported rmtree"),
        ('def test():\n    open("/etc/passwd", "w").write("x")\n', "open for writing"),
    ],
)
def test_h1_dangerous_calls_are_refused_however_they_are_spelled(source: str, label: str) -> None:
    """Before: only ``os.system(...)`` written literally was caught."""
    assert scan_for_dangerous_calls(source), f"{label} reached the host"


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ('def test():\n    data = open("f.txt").read()\n', "reading a file"),
        ('def test(tmp_path):\n    (tmp_path / "a.json").write_text("{}")\n', "fixture write"),
        ('def test(tmp_path):\n    make(tmp_path).write_text("a")\n', "fixture via helper"),
        ('import json\ndef test():\n    json.loads("{}")\n', "json.loads"),
        ("def test():\n    x = my_ctypes_helper()\n", "identifier containing ctypes"),
    ],
)
def test_h1_legitimate_test_code_is_not_refused(source: str, label: str) -> None:
    """A false refusal blocks every build, so the other direction matters too."""
    assert not scan_for_dangerous_calls(source), f"{label} was wrongly refused"


# --------------------------------------------------------------------------
# H-2: "no issues" prose was read without reading the rest of the sentence.
# --------------------------------------------------------------------------


def test_h2_a_negation_after_the_phrase_is_not_a_clean_bill_of_health() -> None:
    """Before: only text *before* the match was scanned for negation."""
    text = "Are there no vulnerabilities? Absolutely not - I found 4 criticals."
    assert reports_no_issues(text) is False


def test_h2_a_question_is_not_an_assertion() -> None:
    assert reports_no_issues("Were there no findings?") is False


def test_h2_a_genuine_clean_report_still_reads_as_clean() -> None:
    assert reports_no_issues("Reviewed thoroughly. No security issues found.") is True


# --------------------------------------------------------------------------
# M-3: reviewers summarise in markdown tables.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("| Final Score | 88 |", "88"),
        ("| Score | 91/100 |", "91"),
        ("**Final Score: 92/100**", "92"),
    ],
)
def test_m3_table_and_prose_verdicts_both_parse(text: str, expected: str) -> None:
    """Before: a table row scored 0 and cost a full extra fix cycle."""
    assert REVIEW_SCORE_RE.findall(text)[-1] == expected


@pytest.mark.parametrize("text", ["Score 3 major problems remain", "The score was good"])
def test_m3_prose_mentioning_score_is_still_not_a_verdict(text: str) -> None:
    assert REVIEW_SCORE_RE.findall(text) == []


# --------------------------------------------------------------------------
# M-4: --cpus was derived from the CPU-time budget.
# --------------------------------------------------------------------------


def test_m4_cpu_shares_no_longer_scale_with_the_time_budget() -> None:
    """A longer budget must not mean a *more powerful* sandbox.

    Before: ``--cpus`` was ``cpu_seconds // 60``, so a 600-second budget
    handed the container ten whole CPUs.
    """
    argv = docker_argv(
        ["python", "-m", "pytest"],
        cwd="/work",
        image="img",
        network="none",
        cpu_seconds=600,
        rss_bytes=1,
    )
    assert "--cpus=1" in argv
    assert "--ulimit=cpu=600" in argv


def test_m4_cpu_shares_are_independently_configurable() -> None:
    argv = docker_argv(
        ["python", "-m", "pytest"],
        cwd="/work",
        image="img",
        network="none",
        cpu_seconds=60,
        rss_bytes=1,
        cpu_shares=2.5,
    )
    assert "--cpus=2.5" in argv


# --------------------------------------------------------------------------
# H-3: accepted builds were queued without limit.
# --------------------------------------------------------------------------


def test_h3_the_build_queue_applies_backpressure() -> None:
    """Past MAX_QUEUED_BUILDS the daemon says 503 instead of queueing forever.

    Before: the rate limiter bounded requests per minute but nothing bounded
    the *work* they created. Builds are serialised by a lock, so accepted
    requests piled up as coroutines that would run for hours after the caller
    had gone.
    """
    from looper.config import HTTPConfig
    from looper.server import HTTPServer

    from .conftest import FakeRequest, FakeWebModule

    started = asyncio.Event()

    async def _slow_build(goal: str) -> None:
        started.set()
        await asyncio.sleep(3600)

    server = HTTPServer(
        HTTPConfig(rate_limit_per_minute=1000),
        _slow_build,
        web_module=FakeWebModule(),
    )

    async def _flood() -> list[int]:
        statuses = []
        for _ in range(server.MAX_QUEUED_BUILDS + 3):
            response = await server.handle_build(FakeRequest({"goal": "x"}))
            statuses.append(response.status)
        for task in list(server._tasks):
            task.cancel()
        return statuses

    statuses = asyncio.run(_flood())
    assert statuses[: server.MAX_QUEUED_BUILDS] == [200] * server.MAX_QUEUED_BUILDS
    assert statuses[server.MAX_QUEUED_BUILDS :] == [503, 503, 503]


def test_h3_a_finished_build_frees_a_queue_slot() -> None:
    """Backpressure must be temporary, not a permanent ceiling."""
    from looper.config import HTTPConfig
    from looper.server import HTTPServer

    from .conftest import FakeRequest, FakeWebModule

    async def _instant_build(goal: str) -> None:
        return None

    server = HTTPServer(
        HTTPConfig(rate_limit_per_minute=1000),
        _instant_build,
        web_module=FakeWebModule(),
    )

    async def _drive() -> int:
        for _ in range(server.MAX_QUEUED_BUILDS * 3):
            response = await server.handle_build(FakeRequest({"goal": "x"}))
            assert response.status == 200
            await asyncio.sleep(0)
        return len(server._tasks)

    assert asyncio.run(_drive()) < server.MAX_QUEUED_BUILDS


# --------------------------------------------------------------------------
# M-6: a truncated module still reported build_ok.
# --------------------------------------------------------------------------


def test_m6_truncated_code_is_reported_and_fails_the_build(raw_config: Any) -> None:
    """A module that lost its tail is not a build that succeeded.

    Before: ``write_file`` truncated silently, the marker was a *comment* so
    lint could not catch it, and the remaining prefix usually still parsed --
    so ``build_ok`` described code the builder never wrote.
    """
    from looper.config import build_config

    from .test_phases import build_phases

    config = build_config({**raw_config, "execution": {"max_file_bytes": 2048}}, env={})
    phases = build_phases(config)

    phases.write_file("src/generated_code.py", "x = 1\n" * 10_000)
    assert phases.was_truncated("src/generated_code.py") is True

    phases.write_file("src/generated_code.py", "print('ok')\n")
    assert phases.was_truncated("src/generated_code.py") is False


def test_m6_truncation_state_is_per_instance(raw_config: Any) -> None:
    """A class-level mutable would leak truncation across daemons."""
    from looper.config import build_config

    from .test_phases import build_phases

    config = build_config({**raw_config, "execution": {"max_file_bytes": 2048}}, env={})
    first = build_phases(config)
    second = build_phases(config)

    first.write_file("src/generated_code.py", "x = 1\n" * 10_000)

    assert first.was_truncated("src/generated_code.py") is True
    assert second.was_truncated("src/generated_code.py") is False


# --------------------------------------------------------------------------
# Coverage of the branches the fixes introduced.
# --------------------------------------------------------------------------


def test_run_build_fails_closed_when_the_module_is_truncated(raw_config: Any) -> None:
    """The full build path, not just the write_file bookkeeping."""
    from looper.config import build_config

    from .conftest import DEFAULT_REPLIES
    from .test_phases import build_phases

    config = build_config(
        {**raw_config, "execution": {"max_file_bytes": 1024}},
        env={},
    )
    phases = build_phases(
        config,
        replies={**DEFAULT_REPLIES, "Code Builder": "print('ok')\n" + "# pad\n" * 500},
    )
    result = asyncio.run(phases.run_build("goal"))
    assert result.build_ok is False
    assert "truncated" in result.summary


def test_zero_argument_call_receiver_resolves_through_the_callable() -> None:
    """``tmp_path.mkdir().write_text(...)`` still roots at the fixture."""
    src = 'def test(tmp_path):\n    tmp_path.joinpath("a").write_text("x")\n'
    assert not scan_for_dangerous_calls(src)


def test_a_call_returning_a_path_from_no_fixture_is_refused() -> None:
    """An unresolvable receiver must not be treated as sandboxed."""
    src = 'def test():\n    somewhere().write_text("x")\n'
    assert scan_for_dangerous_calls(src)


def test_open_mode_passed_as_a_keyword_is_seen() -> None:
    src = 'def test():\n    open("f.txt", mode="w").write("x")\n'
    assert scan_for_dangerous_calls(src)


def test_open_with_a_non_string_mode_is_not_treated_as_a_write() -> None:
    """A dynamic mode is not proof of a write; other checks still apply."""
    src = "def test():\n    m = 'r'\n    open('f.txt', m).read()\n"
    assert not scan_for_dangerous_calls(src)


def test_open_with_a_non_string_keyword_mode_is_ignored() -> None:
    src = "def test():\n    open('f.txt', mode=0).read()\n"
    assert not scan_for_dangerous_calls(src)


def test_deeply_nested_receiver_chain_terminates() -> None:
    """The bounded walk must not hang on a pathological chain."""
    chain = "a" + "".join(f".b{i}" for i in range(50))
    src = f"def test():\n    {chain}.write_text('x')\n"
    assert scan_for_dangerous_calls(src)


def test_open_scans_every_keyword_not_just_the_first() -> None:
    """``open(f, encoding=..., mode="w")`` must still register as a write."""
    src = "def test():\n    open('f.txt', encoding='utf-8', mode='w').write('x')\n"
    assert scan_for_dangerous_calls(src)
