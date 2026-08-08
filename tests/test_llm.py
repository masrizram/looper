"""LLM client: retries, backoff, cancellation, explicit failure signalling."""

from __future__ import annotations

import asyncio

import pytest

from looper.config import AgentSpec, OpenRouterConfig, RetryPolicy
from looper.llm import AgentReply, LLMUnavailableError, OpenRouterClient

from .conftest import FakeSDKClient

AGENT = AgentSpec(model="test/model", role="Test Role", temperature=0.1, max_tokens=64)
FAST_RETRY = RetryPolicy(max_attempts=3, backoff_base=1.0, backoff_max=0.0)


def make(replies=None, fail_with=None, retry=FAST_RETRY) -> OpenRouterClient:
    sdk = FakeSDKClient(replies or {"Test Role": "hello"}, fail_with)
    return OpenRouterClient(OpenRouterConfig(), retry, client=sdk)


def test_successful_call_returns_ok_reply():
    client = make()
    reply = asyncio.run(client.call(AGENT, "prompt"))
    assert reply.ok is True
    assert reply.failed is False
    assert reply.text == "hello"
    assert reply.attempts == 1


def test_system_prompt_carries_the_role():
    client = make()
    asyncio.run(client.call(AGENT, "prompt"))
    system = client._client.completions.calls[0]["messages"][0]["content"]
    assert "Test Role" in system


def test_extra_system_is_appended():
    client = make()
    asyncio.run(client.call(AGENT, "p", extra_system="Be skeptical."))
    system = client._client.completions.calls[0]["messages"][0]["content"]
    assert system.endswith("Be skeptical.")


def test_sampling_settings_are_forwarded():
    client = make()
    asyncio.run(client.call(AGENT, "p"))
    call = client._client.completions.calls[0]
    assert call["model"] == "test/model"
    assert call["temperature"] == 0.1
    assert call["max_tokens"] == 64


def test_failure_retries_then_reports_failure():
    client = make(fail_with=RuntimeError("api down"))
    reply = asyncio.run(client.call(AGENT, "p"))
    assert reply.ok is False
    assert reply.failed is True
    assert reply.attempts == 3
    assert "api down" in reply.error
    assert reply.text.startswith("[ERROR")
    assert len(client._client.completions.calls) == 3


def test_single_attempt_policy_does_not_retry():
    client = make(
        fail_with=RuntimeError("nope"),
        retry=RetryPolicy(max_attempts=1, backoff_base=1.0, backoff_max=0.0),
    )
    reply = asyncio.run(client.call(AGENT, "p"))
    assert reply.attempts == 1
    assert len(client._client.completions.calls) == 1


def test_backoff_sleeps_between_attempts(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = make(
        fail_with=RuntimeError("x"),
        retry=RetryPolicy(max_attempts=3, backoff_base=2.0, backoff_max=60.0),
    )
    asyncio.run(client.call(AGENT, "p"))
    assert slept == [2.0, 4.0]  # no sleep after the final attempt


def test_cancellation_propagates_and_is_not_retried():
    """The old broad `except Exception` swallowed CancelledError in 3.11,
    making the daemon impossible to shut down cleanly mid-retry."""
    client = make(fail_with=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client.call(AGENT, "p"))
    assert len(client._client.completions.calls) == 1


def test_none_content_becomes_empty_string():
    class NoneContentSDK(FakeSDKClient):
        pass

    sdk = NoneContentSDK({})

    async def create(**kwargs):
        class M:
            content = None

        class C:
            message = M()

        class R:
            choices = [C()]

        return R()

    sdk.completions.create = create
    client = OpenRouterClient(OpenRouterConfig(), FAST_RETRY, client=sdk)
    reply = asyncio.run(client.call(AGENT, "p"))
    assert reply.text == ""
    assert reply.ok is True


def test_missing_api_key_is_logged_loudly(caplog):
    """Fail fast instead of burning three retries per phase on 401s."""

    class FakeSDK:
        @staticmethod
        def AsyncOpenAI(**kwargs):  # noqa: N802
            return FakeSDKClient({})

    OpenRouterClient(OpenRouterConfig(api_key=""), FAST_RETRY, sdk=FakeSDK)
    assert "No OpenRouter API key" in caplog.text


def test_api_key_present_is_not_warned(caplog):
    class FakeSDK:
        @staticmethod
        def AsyncOpenAI(**kwargs):  # noqa: N802
            return FakeSDKClient({})

    OpenRouterClient(OpenRouterConfig(api_key="sk-or-x"), FAST_RETRY, sdk=FakeSDK)
    assert "No OpenRouter API key" not in caplog.text


def test_sdk_receives_base_url_and_headers():
    captured = {}

    class FakeSDK:
        @staticmethod
        def AsyncOpenAI(**kwargs):  # noqa: N802
            captured.update(kwargs)
            return FakeSDKClient({})

    cfg = OpenRouterConfig(api_key="k", site_url="https://x.dev", site_name="Looper")
    OpenRouterClient(cfg, FAST_RETRY, sdk=FakeSDK)
    assert captured["base_url"] == cfg.base_url
    assert captured["default_headers"]["X-Title"] == "Looper"


def test_headers_none_when_empty():
    captured = {}

    class FakeSDK:
        @staticmethod
        def AsyncOpenAI(**kwargs):  # noqa: N802
            captured.update(kwargs)
            return FakeSDKClient({})

    OpenRouterClient(
        OpenRouterConfig(api_key="k", site_url="", site_name=""), FAST_RETRY, sdk=FakeSDK
    )
    assert captured["default_headers"] is None


def test_agent_reply_dataclass_is_frozen():
    reply = AgentReply(text="x", ok=True, attempts=1)
    with pytest.raises(Exception):
        reply.text = "y"


def test_llm_unavailable_error_exists():
    assert issubclass(LLMUnavailableError, RuntimeError)
