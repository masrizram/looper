"""Proof tests for package (multi-file) builds end to end (ADR-009).

These drive the real ``PhaseManager.run_build`` with a fake LLM reply, so they
prove the files actually land on disk, that unsafe paths are refused, and that
``build_ok`` still means *the code parses*.
"""

from __future__ import annotations

import asyncio

import pytest

from looper.config import build_config
from looper.phases import CODE_FILE, PhaseManager
from looper.state import StateManager
from tests.conftest import DEFAULT_REPLIES, make_client

PACKAGE_REPLY = """I split this into modules.

### FILE: src/app/models.py
```python
class User:
    def __init__(self, name):
        self.name = name
```

### FILE: src/app/service.py
```python
def greet(user):
    return "hi " + user.name
```

### FILE: README.md
```markdown
# Generated app
```
"""


def _package_manager(raw_config, reply: str, **execution):
    cfg = build_config(
        {
            **raw_config,
            "execution": {
                "artifact_mode": "package",
                "lint_generated": "off",
                **execution,
            },
        },
        env={},
    )
    replies = {**DEFAULT_REPLIES, "Code Builder": reply}
    state = StateManager(cfg.state_file, cfg.execution.max_history_entries)
    return cfg, PhaseManager(cfg, state, make_client(cfg, replies))


def test_package_build_writes_every_file(raw_config):
    cfg, phases = _package_manager(raw_config, PACKAGE_REPLY)
    result = asyncio.run(phases.run_build("goal"))

    assert result.build_ok is True
    assert "3 files" in result.summary

    workspace = phases.workspace
    assert (workspace / "src/app/models.py").read_text(encoding="utf-8").startswith("class User")
    assert (workspace / "src/app/service.py").exists()
    assert (workspace / "README.md").exists()


def test_reviewer_sees_every_module_not_just_the_first(raw_config):
    """A vulnerability in module 2 must not escape the audit."""
    _, phases = _package_manager(raw_config, PACKAGE_REPLY)
    asyncio.run(phases.run_build("goal"))

    audited = phases.read_file(CODE_FILE)
    assert "class User" in audited
    assert "def greet" in audited


def test_package_build_fails_closed_on_syntax_error(raw_config):
    reply = "### FILE: src/broken.py\n```python\ndef oops(:\n    pass\n```\n"
    _, phases = _package_manager(raw_config, reply)
    result = asyncio.run(phases.run_build("goal"))

    assert result.build_ok is False
    assert "syntax error" in result.summary


def test_package_with_no_python_files_fails_closed(raw_config):
    reply = "### FILE: README.md\n```markdown\n# only docs\n```\n"
    _, phases = _package_manager(raw_config, reply)
    result = asyncio.run(phases.run_build("goal"))

    assert result.build_ok is False
    assert "no Python files" in result.summary


def test_package_mode_falls_back_to_single_file_without_markers(raw_config):
    """Enabling package mode must never break a plain single-file build."""
    _, phases = _package_manager(raw_config, "```python\nx = 1\n```")
    result = asyncio.run(phases.run_build("goal"))

    assert result.build_ok is True
    assert phases.read_file(CODE_FILE).strip() == "x = 1"


def test_package_file_count_is_capped_on_disk(raw_config):
    reply = "".join(f"### FILE: m{i}.py\n```python\nx = {i}\n```\n" for i in range(8))
    _, phases = _package_manager(raw_config, reply, max_files_per_build=3)
    result = asyncio.run(phases.run_build("goal"))

    assert result.build_ok is True
    written = sorted(p.name for p in phases.workspace.glob("m*.py"))
    assert written == ["m0.py", "m1.py", "m2.py"]


def test_package_refuses_path_escaping_the_workspace(raw_config, monkeypatch):
    """Defence in depth: even if the parser were bypassed, the sink refuses."""
    from looper import artifact as artifact_module

    monkeypatch.setattr(artifact_module, "is_safe_relative_path", lambda path: True)
    reply = "### FILE: ../../escaped.py\n```python\nx = 1\n```\n"
    _, phases = _package_manager(raw_config, reply)
    result = asyncio.run(phases.run_build("goal"))

    assert result.build_ok is False
    assert "refused unsafe artifact path" in result.summary
    assert not (phases.workspace.parent.parent / "escaped.py").exists()


def test_package_lint_failure_fails_the_build(raw_config):
    # flake8 gate on a file with an unused import + bad spacing.
    reply = "### FILE: bad.py\n```python\nimport os\nx=1\n```\n"
    _, phases = _package_manager(raw_config, reply, lint_generated="flake8")
    result = asyncio.run(phases.run_build("goal"))

    assert result.build_ok is False
    assert "flake8" in result.summary


def test_package_build_skipped_when_agent_failed(raw_config):
    cfg = build_config(
        {**raw_config, "execution": {"artifact_mode": "package", "lint_generated": "off"}},
        env={},
    )
    state = StateManager(cfg.state_file, cfg.execution.max_history_entries)
    client = make_client(cfg, None, fail_with=RuntimeError("llm down"))
    phases = PhaseManager(cfg, state, client)
    result = asyncio.run(phases.run_build("goal"))

    assert result.build_ok is False


@pytest.mark.parametrize("mode", ["single_file", "package"])
def test_both_modes_reject_empty_output(raw_config, mode):
    cfg = build_config(
        {**raw_config, "execution": {"artifact_mode": mode, "lint_generated": "off"}}, env={}
    )
    state = StateManager(cfg.state_file, cfg.execution.max_history_entries)
    phases = PhaseManager(cfg, state, make_client(cfg, {**DEFAULT_REPLIES, "Code Builder": "  "}))
    result = asyncio.run(phases.run_build("goal"))

    assert result.build_ok is False


def test_builder_prompt_teaches_the_marker_only_in_package_mode():
    """The parser recognises one syntax; the prompt must name it exactly.

    Without this the builder never emits markers and package mode silently
    degrades to single-file forever.
    """
    from looper.prompts import PromptGenerator

    single = PromptGenerator.build("goal", "arch")
    packaged = PromptGenerator.build("goal", "arch", package_mode=True)

    assert "### FILE:" not in single
    assert "### FILE: relative/path/to/file.py" in packaged
    assert "no '..'" in packaged


def test_package_mode_prompt_is_actually_used(raw_config):
    cfg, phases = _package_manager(raw_config, PACKAGE_REPLY)
    asyncio.run(phases.run_build("goal"))
    calls = phases.client._client.completions.calls  # type: ignore[attr-defined]
    user_messages = [c["messages"][-1]["content"] for c in calls]
    assert any("### FILE: relative/path/to/file.py" in m for m in user_messages)
