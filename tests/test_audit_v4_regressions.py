"""Regressions for the fourth audit round, plus a two-way calibration corpus.

Every finding closed here shared one cause: a heuristic was tested only in the
direction it was designed to catch, never in the direction it could break. The
581-test suite was at 100% line and branch coverage and found none of them,
because coverage proves a line *ran*, not that its threshold was right.

So these tests come in pairs. Each one that proves something is refused has a
sibling proving the legitimate form is still accepted, and vice versa. The
calibration corpus at the bottom is the general form of that rule: benign
suites that must pass, hostile payloads that must be refused, run as one
parametrized sweep so widening a pattern table cannot quietly start rejecting
real code.
"""

from __future__ import annotations

import asyncio

import pytest

from looper.adequacy import evaluate_suite
from looper.config import AgentSpec, CostBudgetExceeded, OpenRouterConfig, RetryPolicy
from looper.llm import OpenRouterClient
from looper.phases.agents import REVIEW_SCORE_RE
from looper.phases.workspace import strip_code_fences
from looper.sandbox import docker_argv, scan_for_dangerous_calls, scan_source
from looper.scoring import reports_no_issues

# --- H-1: filesystem writes through an isolated fixture ---------------------

TMP_PATH_SUITE = (
    "import json\n"
    "\n"
    "def test_roundtrip(tmp_path):\n"
    "    p = tmp_path / 'd.json'\n"
    "    p.write_text(json.dumps({'a': 1}), encoding='utf-8')\n"
    "    assert json.loads(p.read_text()) == {'a': 1}\n"
)


def test_h1_writes_through_tmp_path_are_allowed():
    """tmp_path is the most common fixture in pytest; refusing it meant no
    build whose tests touched disk could ever clear the gate."""
    assert scan_for_dangerous_calls(TMP_PATH_SUITE) == []


def test_h1_tmp_path_write_is_recorded_as_a_warning_not_a_refusal():
    verdict = scan_source(TMP_PATH_SUITE)
    assert verdict.refused is False
    # The receiver is the local alias 'p', resolved back to the tmp_path root.
    assert any("write_text" in note for note in verdict.warn)


def test_h1_alias_chain_from_a_fixture_is_still_confined():
    src = (
        "def test_c(tmp_path):\n"
        "    d = tmp_path / 'sub'\n"
        "    d.mkdir()\n"
        "    f = d / 'x.txt'\n"
        "    f.write_text('hi')\n"
    )
    assert scan_for_dangerous_calls(src) == []


def test_h1_the_same_method_on_a_real_path_is_still_refused():
    """The other direction: relaxing the fixture case must not relax writes to
    an arbitrary host path."""
    src = "from pathlib import Path\np = Path('/etc/passwd')\np.write_text('pwn')\n"
    assert scan_for_dangerous_calls(src) != []


def test_h1_a_fixture_in_scope_does_not_launder_an_unrelated_write():
    src = "from pathlib import Path\ndef test_x(tmp_path):\n    Path('/etc/shadow').unlink()\n"
    assert scan_for_dangerous_calls(src) != []


# --- H-2: string literals must not trip the substring pass ------------------


def test_h2_a_docstring_mentioning_socket_is_not_a_refusal():
    src = (
        "def test_doc():\n"
        '    """We must never use socket. connections here."""\n'
        "    assert 1 == 1\n"
    )
    assert scan_for_dangerous_calls(src) == []


def test_h2_a_real_socket_call_is_still_refused():
    src = "import socket\ndef test_n():\n    socket.socket()\n"
    assert scan_for_dangerous_calls(src) != []


def test_h2_a_hash_inside_a_string_does_not_hide_a_dangerous_call():
    """The original bug in the other direction: truncating at '#' swallowed
    everything after a URL fragment."""
    src = "import os\nurl = 'http://x/#f'\nos.system('pwn')\n"
    assert scan_for_dangerous_calls(src) != []


# --- L-2: getattr indirection, both forms -----------------------------------


def test_l2_ordinary_getattr_is_allowed():
    assert scan_for_dangerous_calls("x = getattr(o, 'name', None)\n") == []


def test_l2_computed_attribute_name_is_refused():
    assert scan_for_dangerous_calls("x = getattr(o, n)\n") != []


def test_l2_literal_lookup_into_a_dangerous_module_is_still_refused():
    """A literal name is not evidence of innocence when the receiver is os."""
    assert scan_for_dangerous_calls("import os\ngetattr(os, 'system')('ls')\n") != []


# --- H-3: the cost ceiling is enforced at the point of spend ----------------


class _Usage:
    prompt_tokens = 100_000
    completion_tokens = 100_000


class _Msg:
    content = "ok"


class _Choice:
    message = _Msg()


class _Resp:
    choices = [_Choice()]
    usage = _Usage()


class _Completions:
    async def create(self, **_kwargs):
        return _Resp()


class _Chat:
    completions = _Completions()


class _Client:
    chat = _Chat()


def _capped_client(max_cost_usd: float) -> OpenRouterClient:
    return OpenRouterClient(
        OpenRouterConfig(),
        RetryPolicy(),
        client=_Client(),
        model_prices_usd_per_1k={"anthropic/claude-opus-5": 0.015},
        max_cost_usd=max_cost_usd,
    )


def test_h3_budget_stops_mid_cycle_instead_of_at_the_next_cycle_boundary():
    """One cycle is seven agent calls. Checking only between cycles let a
    $1.00 budget reach $18.00 before the guard was consulted."""
    client = _capped_client(1.00)
    agent = AgentSpec("anthropic/claude-opus-5", "Researcher")

    async def _spend() -> float:
        await client.call(agent, "hi")  # $3.00, over the cap
        with pytest.raises(CostBudgetExceeded):
            await client.call(agent, "hi")
        return client.running_cost_usd()

    assert asyncio.run(_spend()) == pytest.approx(3.0)


def test_h3_a_zero_budget_disables_the_cap():
    client = _capped_client(0.0)
    agent = AgentSpec("anthropic/claude-opus-5", "Researcher")

    async def _spend() -> float:
        for _ in range(3):
            await client.call(agent, "hi")
        return client.running_cost_usd()

    assert asyncio.run(_spend()) == pytest.approx(9.0)


# --- H-4: the review score regex ---------------------------------------------


def _parse_score(text: str) -> float | None:
    matches = REVIEW_SCORE_RE.findall(text)
    return float(matches[-1]) if matches else None


@pytest.mark.parametrize(
    "prose",
    [
        "I found 3 issues. Score 3 major problems remain",
        "The code scores 100 in readability",
        "score=7 out of 10 for style",
    ],
)
def test_h4_prose_containing_the_word_score_is_not_a_verdict(prose: str):
    assert _parse_score(prose) is None


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("blah\nScore: 88\n", 88.0),
        ("**Final Score: 92/100**", 92.0),
        ("Score = 75", 75.0),
        ("Score: 40\nafter fixes\n**Final Score: 92/100**", 92.0),
    ],
)
def test_h4_a_real_verdict_line_is_parsed(reply: str, expected: float):
    assert _parse_score(reply) == expected


# --- M-1/M-2/M-3: the adequacy gate across frameworks ------------------------


def test_m1_a_unittest_suite_is_not_rated_at_zero_assertions():
    src = (
        "import unittest\n"
        "from src.generated_code import add\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(1, 2), 3)\n"
        "    def test_neg(self):\n"
        "        self.assertEqual(add(-1, -1), -2)\n"
    )
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.ok is True, report.reason
    assert report.assertion_statements == 2


def test_m2_pytest_raises_counts_as_an_assertion():
    src = (
        "import pytest\n"
        "from src.generated_code import div\n"
        "def test_zero():\n"
        "    with pytest.raises(ZeroDivisionError):\n"
        "        div(1, 0)\n"
    )
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.assertion_statements == 1
    assert report.ok is True, report.reason


def test_m3_a_tautological_suite_is_refused_however_dense():
    """Density measures effort, not verification: `assert 1 == 1` three times
    scores 75 assertions/100 lines and tests nothing."""
    src = "def test_a():\n    assert 1 == 1\n    assert 2 == 2\n    assert 3 == 3\n"
    report = evaluate_suite(src, min_assertions_per_100_lines=6)
    assert report.ok is False
    assert report.imports_subject is False


def test_m3_importing_only_the_test_framework_is_not_a_subject():
    src = "import pytest\ndef test_a():\n    assert 1 == 1\n    assert 2 == 2\n"
    assert evaluate_suite(src, min_assertions_per_100_lines=6).ok is False


# --- M-4: negated clean-bill-of-health --------------------------------------


def test_m4_a_negated_clean_phrase_is_not_a_clean_report():
    text = "It is not true that there are no vulnerabilities; see below."
    assert reports_no_issues(text) is False


def test_m4_a_genuine_clean_report_still_registers():
    assert reports_no_issues("No issues found.") is True


def test_m4_a_negation_on_another_line_does_not_suppress_a_clean_line():
    assert reports_no_issues("This is not a full audit.\nNo issues found.") is True


# --- M-6: container CPU-time cap --------------------------------------------


def test_m6_container_gets_a_real_cpu_time_limit_not_only_a_share():
    argv = docker_argv(
        ["/usr/bin/python"],
        cwd="/w",
        image="img",
        network="none",
        cpu_seconds=30,
        rss_bytes=10**9,
    )
    assert "--ulimit=cpu=30" in argv
    assert "--cpus=1" in argv


# --- L-1: fenced-block selection ---------------------------------------------


def test_l1_the_real_module_wins_over_a_leading_example_snippet():
    reply = (
        "First a tiny example:\n"
        "```python\n"
        "print('hi')\n"
        "```\n"
        "And here is the actual module:\n"
        "```python\n"
        "def main():\n"
        "    return 42\n"
        "\n"
        "def helper():\n"
        "    return 1\n"
        "```\n"
    )
    assert "def main()" in strip_code_fences(reply)
    assert strip_code_fences(reply) != "print('hi')"


def test_l1_a_single_block_is_unchanged():
    assert strip_code_fences("text\n```python\nx = 1\n```") == "x = 1"


def test_l1_unfenced_text_is_returned_as_is():
    assert strip_code_fences("x = 1") == "x = 1"


# --- A-5: two-way calibration corpus -----------------------------------------
#
# Coverage cannot prove a threshold is correct, only that it executed. These
# two lists are the actual specification of the scanner's behaviour: widening
# a pattern table without checking the BENIGN column is how H-1 happened.

BENIGN_SUITES: tuple[tuple[str, str], ...] = (
    ("plain arithmetic", "def test_a():\n    assert 1 + 1 == 2\n"),
    ("json roundtrip", "import json\ndef test_j():\n    assert json.loads('{}') == {}\n"),
    ("tmp_path write", TMP_PATH_SUITE),
    (
        "tmpdir fixture",
        "def test_t(tmpdir):\n    f = tmpdir / 'a.txt'\n    f.write_text('x', 'utf-8')\n",
    ),
    (
        "pytest.raises",
        "import pytest\ndef test_r():\n    with pytest.raises(ValueError):\n        int('x')\n",
    ),
    (
        "docstring naming dangerous apis",
        'def test_d():\n    """Never call os.system or socket. here."""\n    assert True\n',
    ),
    ("getattr with default", "def test_g():\n    assert getattr(object(), 'x', 1) == 1\n"),
    ("bare import of subprocess", "import subprocess\ndef test_i():\n    assert subprocess\n"),
    (
        "dataclass under test",
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class P:\n"
        "    x: int\n"
        "def test_p():\n"
        "    assert P(1).x == 1\n",
    ),
    ("mkdir on tmp_path", "def test_m(tmp_path):\n    (tmp_path / 'd').mkdir()\n"),
)

HOSTILE_PAYLOADS: tuple[tuple[str, str], ...] = (
    ("os.system", "import os\nos.system('rm -rf /')\n"),
    ("os.popen", "import os\nos.popen('id')\n"),
    ("subprocess.run", "import subprocess\nsubprocess.run(['id'])\n"),
    ("subprocess.Popen", "import subprocess\nsubprocess.Popen(['id'])\n"),
    ("shutil.rmtree", "import shutil\nshutil.rmtree('/')\n"),
    ("socket", "import socket\nsocket.socket()\n"),
    ("eval", "eval('1+1')\n"),
    ("exec", "exec('x=1')\n"),
    ("pickle.loads", "import pickle\npickle.loads(b'')\n"),
    ("ctypes", "import ctypes\nctypes.CDLL('libc.so.6')\n"),
    ("getattr indirection", "import os\ngetattr(os, 'system')('ls')\n"),
    ("computed getattr", "x = getattr(mod, name)\n"),
    ("write outside workspace", "from pathlib import Path\nPath('/etc/x').write_text('p')\n"),
    ("unlink a real path", "from pathlib import Path\nPath('/etc/x').unlink()\n"),
    ("fork", "import os\nos.fork()\n"),
    ("outbound http", "import requests\nrequests.get('http://x')\n"),
    ("dynamic import", "__import__('os').system('id')\n"),
    ("marshal", "import marshal\nmarshal.loads(b'')\n"),
    ("os.remove", "import os\nos.remove('/etc/passwd')\n"),
    ("chmod a real path", "from pathlib import Path\nPath('/etc/x').chmod(0o777)\n"),
)


@pytest.mark.parametrize("label,source", BENIGN_SUITES, ids=[c[0] for c in BENIGN_SUITES])
def test_calibration_benign_suites_are_not_refused(label: str, source: str):
    reasons = scan_for_dangerous_calls(source)
    assert reasons == [], f"{label} was refused: {reasons}"


@pytest.mark.parametrize("label,source", HOSTILE_PAYLOADS, ids=[c[0] for c in HOSTILE_PAYLOADS])
def test_calibration_hostile_payloads_are_refused(label: str, source: str):
    assert scan_for_dangerous_calls(source) != [], f"{label} was allowed through"
