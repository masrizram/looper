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
        {
            **raw_config,
            "execution": {
                "user_tests_dir": str(tmp_path / "ut"),
                "sandbox_tests": False,
            },
        },
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
    # run_build first: the generated suite now imports the module under test
    # (as the test prompt demands), so without the artifact on disk it fails
    # collection and run_test never reaches the propagation path.
    asyncio.run(phases.run_build("goal"))

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


def test_running_cost_starts_at_zero_and_accumulates_per_call():
    """#1: spend is accumulated per call, never derived from total_usage.

    Deriving it afterwards priced every token at the generic default,
    under-reporting an Opus run by ~7.5x and making max_cost_usd a budget
    in name only.
    """
    client = OpenRouterClient.__new__(OpenRouterClient)
    client._default_price = 0.002
    client._model_prices = {}
    client._cost_usd = 0.0
    client._cost_by_model = {}
    client.total_usage = TokenUsage(prompt_tokens=1000, completion_tokens=0)
    # Usage alone must NOT create spend: only a recorded call does.
    assert client.running_cost_usd() == pytest.approx(0.0)


def test_running_cost_prices_each_model_separately():
    """#1: an Opus call and a cheap call must not cost the same per token."""
    import asyncio as _asyncio

    from looper.config import AgentSpec, OpenRouterConfig, RetryPolicy

    class _Msg:
        def __init__(self, text):
            self.content = text

    class _Choice:
        def __init__(self, text):
            self.message = _Msg(text)

    class _Usage:
        prompt_tokens = 1_000_000
        completion_tokens = 0

    class _Resp:
        choices = [_Choice("ok")]
        usage = _Usage()

    class _Completions:
        async def create(self, **kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _SDKClient:
        chat = _Chat()

    client = OpenRouterClient(
        OpenRouterConfig(),
        RetryPolicy(),
        client=_SDKClient(),
        model_prices_usd_per_1k={"pricey/model": 0.015, "cheap/model": 0.0002},
        default_token_price_usd=0.002,
    )
    _asyncio.run(client.call(AgentSpec("pricey/model", "Researcher"), "x"))
    assert client.running_cost_usd() == pytest.approx(15.0)

    _asyncio.run(client.call(AgentSpec("cheap/model", "Writer"), "x"))
    assert client.running_cost_usd() == pytest.approx(15.2)

    breakdown = client.cost_by_model()
    assert breakdown["pricey/model"] == pytest.approx(15.0)
    assert breakdown["cheap/model"] == pytest.approx(0.2)


def test_model_price_per_1k_falls_back_to_default():
    """#1: an unknown model uses the configured default price."""
    client = OpenRouterClient.__new__(OpenRouterClient)
    client._default_price = 0.005
    client._model_prices = {"known/model": 0.01}
    assert client.model_price_per_1k("unknown/model") == pytest.approx(0.005)
    assert client.model_price_per_1k("known/model") == pytest.approx(0.01)
