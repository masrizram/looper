"""LLM client: retries, backoff, cancellation, explicit failure signalling."""

from __future__ import annotations

import asyncio

import pytest

from looper.config import AgentSpec, OpenRouterConfig, RetryPolicy
from looper.llm import (
    AgentReply,
    LLMUnavailableError,
    NonRetryableError,
    OpenRouterClient,
    TokenUsage,
    is_retryable,
    status_code_of,
    usage_of,
)

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


# --- GAP-1: per-call timeout -------------------------------------------------


def test_hanging_call_times_out_instead_of_wedging_the_daemon():
    """A stalled connection previously hung the phase, and thus the daemon,
    forever: the subprocess had a timeout but the LLM call did not."""

    class HangingSDK:
        class completions:
            calls = 0

            @staticmethod
            async def create(**kwargs):
                HangingSDK.completions.calls += 1
                await asyncio.sleep(3600)

        chat = type("C", (), {"completions": completions})()

    client = OpenRouterClient(
        OpenRouterConfig(api_key="k", request_timeout_seconds=1.0),
        RetryPolicy(max_attempts=1, backoff_base=1.0, backoff_max=0.0),
        client=HangingSDK(),
    )

    async def run():
        return await asyncio.wait_for(client.call(AGENT, "p"), timeout=5)

    reply = asyncio.run(run())
    assert reply.ok is False
    assert reply.timed_out is True


def test_timeout_is_retried():
    """A timeout may be a transient blip, so it consumes the retry budget."""

    class SlowSDK:
        class completions:
            calls = 0

            @staticmethod
            async def create(**kwargs):
                SlowSDK.completions.calls += 1
                await asyncio.sleep(3600)

        chat = type("C", (), {"completions": completions})()

    client = OpenRouterClient(
        OpenRouterConfig(api_key="k", request_timeout_seconds=1.0),
        RetryPolicy(max_attempts=3, backoff_base=1.0, backoff_max=0.0),
        client=SlowSDK(),
    )
    reply = asyncio.run(client.call(AGENT, "p"))
    assert reply.attempts == 3
    assert SlowSDK.completions.calls == 3


def test_timeout_flag_is_false_for_ordinary_errors():
    client = make(fail_with=RuntimeError("boom"))
    assert asyncio.run(client.call(AGENT, "p")).timed_out is False


# --- GAP-2: retry classification --------------------------------------------


class StatusError(RuntimeError):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"Error code: {status}")
        self.status_code = status


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 422])
def test_client_errors_are_not_retried(status):
    """A 401 stays a 401. Retrying burns the budget and delays feedback."""
    client = make(fail_with=StatusError(status))
    reply = asyncio.run(client.call(AGENT, "p"))
    assert reply.ok is False
    assert reply.attempts == 1
    assert len(client._client.completions.calls) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 422, 402])
def test_non_retryable_statuses_are_in_the_non_retryable_set(status):
    """All these statuses are treated as non-retryable by ``is_retryable``."""
    assert is_retryable(StatusError(status)) is False


def test_402_is_reported_as_out_of_credits_and_not_retried():
    """A 402 means the account cannot pay: fail fast with a clear signal."""
    client = make(fail_with=StatusError(402))
    reply = asyncio.run(client.call(AGENT, "p"))
    assert reply.ok is False
    assert reply.out_of_credits is True
    assert reply.attempts == 1
    assert len(client._client.completions.calls) == 1


def test_402_logs_credits_message(caplog):
    client = make(fail_with=StatusError(402))
    asyncio.run(client.call(AGENT, "p"))
    assert "402 Payment Required" in caplog.text


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_errors_are_retried(status):
    client = make(fail_with=StatusError(status))
    reply = asyncio.run(client.call(AGENT, "p"))
    assert reply.attempts == 3


def test_unknown_errors_are_assumed_transient():
    """A bare socket error carries no status; assume a network blip."""
    client = make(fail_with=OSError("connection reset"))
    assert asyncio.run(client.call(AGENT, "p")).attempts == 3


def test_status_parsed_from_message_when_unstructured():
    """The SDK sometimes only puts the code in the message text."""
    client = make(fail_with=RuntimeError("Error code: 401 - Invalid API key"))
    assert asyncio.run(client.call(AGENT, "p")).attempts == 1


@pytest.mark.parametrize(
    "exc,expected",
    [
        (StatusError(404), 404),
        (RuntimeError("Error code: 503 - upstream"), 503),
        (RuntimeError("something went wrong"), None),
    ],
)
def test_status_code_extraction(exc, expected):
    assert status_code_of(exc) == expected


def test_status_code_from_status_attribute():
    exc = RuntimeError("x")
    exc.status = 418
    assert status_code_of(exc) == 418


def test_status_code_from_http_status_attribute():
    exc = RuntimeError("x")
    exc.http_status = 409
    assert status_code_of(exc) == 409


def test_status_code_from_nested_response():
    exc = RuntimeError("x")
    exc.response = type("R", (), {"status_code": 451})()
    assert status_code_of(exc) == 451


def test_status_code_ignores_non_int_attributes():
    exc = RuntimeError("x")
    exc.status_code = "not-an-int"
    assert status_code_of(exc) is None


def test_is_retryable_matrix():
    assert is_retryable(StatusError(429)) is True
    assert is_retryable(StatusError(500)) is True
    assert is_retryable(StatusError(401)) is False
    assert is_retryable(RuntimeError("no status")) is True


# --- GAP-3: token accounting -------------------------------------------------


class UsageSDK:
    def __init__(self, prompt=11, completion=7, omit=False):

        class completions:
            calls = []

            @staticmethod
            async def create(**kwargs):
                completions.calls.append(kwargs)

                class M:
                    content = "hi"

                class C:
                    message = M()

                class R:
                    choices = [C()]

                if not omit:
                    R.usage = type(
                        "U",
                        (),
                        {"prompt_tokens": prompt, "completion_tokens": completion},
                    )()
                return R()

        completions.calls = []
        self.completions = completions
        self.chat = type("C", (), {"completions": completions})()


def test_token_usage_is_recorded():
    client = OpenRouterClient(OpenRouterConfig(api_key="k"), FAST_RETRY, client=UsageSDK())
    reply = asyncio.run(client.call(AGENT, "p"))
    assert reply.usage.prompt_tokens == 11
    assert reply.usage.completion_tokens == 7
    assert reply.usage.total_tokens == 18


def test_token_usage_accumulates_across_calls():
    client = OpenRouterClient(OpenRouterConfig(api_key="k"), FAST_RETRY, client=UsageSDK())

    async def run():
        await client.call(AGENT, "a")
        await client.call(AGENT, "b")

    asyncio.run(run())
    assert client.total_usage.total_tokens == 36
    assert client.call_count == 2


def test_missing_usage_is_tolerated():
    """Not every provider reports usage; absence must not crash the run."""
    client = OpenRouterClient(OpenRouterConfig(api_key="k"), FAST_RETRY, client=UsageSDK(omit=True))
    reply = asyncio.run(client.call(AGENT, "p"))
    assert reply.usage.total_tokens == 0
    assert reply.ok is True


def test_usage_of_ignores_non_int_fields():
    response = type(
        "R", (), {"usage": type("U", (), {"prompt_tokens": None, "completion_tokens": "x"})()}
    )()
    assert usage_of(response).total_tokens == 0


def test_token_usage_addition_and_dict():
    total = TokenUsage(3, 4) + TokenUsage(1, 2)
    assert total.total_tokens == 10
    assert total.as_dict() == {
        "prompt_tokens": 4,
        "completion_tokens": 6,
        "total_tokens": 10,
    }


def test_request_timeout_is_configurable():
    cfg = OpenRouterConfig(request_timeout_seconds=42.0)
    assert cfg.request_timeout_seconds == 42.0


def test_non_retryable_error_class_exists():
    assert issubclass(NonRetryableError, RuntimeError)
