"""The bundled examples must stay valid, or they teach the wrong thing.

Documentation rots silently. A config in ``examples/`` that no longer parses
is worse than no example: a new user copies it, gets exit 2, and concludes the
tool is broken. These tests make the examples part of the gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from looper.config import build_config

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
CONFIGS = sorted(EXAMPLES.glob("*/config.yaml"))


def test_examples_directory_is_populated():
    """Guards against a rename or a deleted folder passing unnoticed."""
    assert CONFIGS, f"no example configs found under {EXAMPLES}"
    assert len(CONFIGS) >= 5


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.parent.name)
def test_example_config_validates(path: Path):
    """Every shipped config must survive the real validator, not just YAML."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = build_config(raw, env={})
    assert config.workspace
    assert config.execution.max_cycles >= 1


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.parent.name)
def test_example_has_a_readme(path: Path):
    readme = path.parent / "README.md"
    assert readme.exists(), f"{path.parent.name} has no README.md"
    assert readme.read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.parent.name)
def test_example_never_hardcodes_a_secret(path: Path):
    """A committed example must not carry a live key.

    ``sk-or-`` is the OpenRouter key prefix. Configs reference the *name* of
    an env var, never a value -- if this ever fails, a real key has leaked
    into the repository.
    """
    text = path.read_text(encoding="utf-8")
    assert "sk-or-" not in text
    assert "api_key:" not in text


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.parent.name)
def test_examples_keep_the_sandbox_closed(path: Path):
    """No example may teach a user to disable the fail-closed sandbox.

    ``sandbox_fail_closed: false`` is a legitimate escape hatch, but it must
    never appear in the copy-paste material -- that is exactly how a
    fail-open default spreads.
    """
    config = build_config(yaml.safe_load(path.read_text(encoding="utf-8")), env={})
    if config.execution.sandbox_tests:
        assert config.execution.sandbox_fail_closed is True


def test_user_tests_example_targets_the_artifact_path():
    """The template suite must import the file the build actually writes."""
    suite = EXAMPLES / "03-user-owned-tests" / "user_tests" / "test_contract.py"
    text = suite.read_text(encoding="utf-8")
    assert "generated_code.py" in text
    # It must skip, not error, when run before a build exists.
    assert "pytest.skip" in text


def test_examples_readme_lists_every_scenario():
    index = (EXAMPLES / "README.md").read_text(encoding="utf-8")
    for path in CONFIGS:
        assert path.parent.name in index, f"{path.parent.name} missing from examples/README.md"
