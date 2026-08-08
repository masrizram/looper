"""Edge-coverage for the new safeguard gates (user test failure propagation,
missing relative user dir, config/llm defaults)."""

from __future__ import annotations

import asyncio

import pytest

from looper.config import ConfigError, build_config
from looper.llm import OpenRouterClient, TokenUsage
from looper.phases import PhaseManager
from looper.state import StateManager
from tests.conftest import DEFAULT_REPLIES, make_client


def test_user_test_failure_propagates_note(tmp_path, raw_config):
    """#3: a user suite that fails to collect must surface a non-empty note that
    propagates into the run_test summary (phases.py:391-392)."""
    config = build_config(
        {**raw_config, "execution": {"user_tests_dir": str(tmp_path / "ut")}},
        env={},
    )
    state = StateManager(config.state_file, config.execution.max_history_entries)
    phases = PhaseManager(
        config,
        state,
        make_client(config, DEFAULT_REPLIES),
        config_dir=tmp_path,
    )

    # Stub the user-suite run so it reports a collection error note; the
    # generated suite itself must pass so run_test reaches the propagation.
    async def _stub_user_tests():
        return 0, 0, "pytest exited 2 with no test summary"

    phases._run_user_tests = _stub_user_tests  # type: ignore[assignment]
    result = asyncio.run(phases.run_test("goal"))
    assert "pytest exited 2" in result.summary


def test_relative_user_dir_missing_is_skipped(tmp_path, raw_config):
    """#3: a configured relative dir that does not exist must skip, not crash."""
    cfg = build_config({**raw_config, "execution": {"user_tests_dir": "does_not_exist"}}, env={})
    phases = PhaseManager(
        cfg,
        None,
        make_client(cfg, DEFAULT_REPLIES),
        config_dir=tmp_path,
    )
    user_passed, user_failed, user_note = asyncio.run(phases._run_user_tests())
    assert (user_passed, user_failed, user_note) == (0, 0, "")


def test_config_rejects_bad_lint_mode():
    """#4: only off|py_compile|flake8 is accepted (config.py:300)."""
    with pytest.raises(ConfigError):
        build_config({"execution": {"lint_generated": "ruff"}}, env={})


def test_running_cost_uses_default_price():
    """#1: cost estimate falls back to the default token price when unset."""
    client = OpenRouterClient.__new__(OpenRouterClient)
    client._default_price = 0.002
    client._model_prices = {}
    client.total_usage = TokenUsage(prompt_tokens=1000, completion_tokens=0)
    assert client.running_cost_usd() == pytest.approx(0.002)


def test_model_price_per_1k_falls_back_to_default():
    """#1: an unknown model uses the configured default price."""
    client = OpenRouterClient.__new__(OpenRouterClient)
    client._default_price = 0.005
    client._model_prices = {"known/model": 0.01}
    assert client.model_price_per_1k("unknown/model") == pytest.approx(0.005)
    assert client.model_price_per_1k("known/model") == pytest.approx(0.01)
