"""OpenRouter LLM client: timeouts, retries, backoff, and cost accounting."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Mapping

from looper.config import AgentSpec, OpenRouterConfig, RetryPolicy

logger = logging.getLogger("looper.llm")

#: Prefix marking a failed agent call. Callers MUST check ``AgentReply.ok``
#: rather than pattern-matching this string.
ERROR_PREFIX = "[ERROR"

#: HTTP statuses that retrying cannot fix. A 401 stays a 401 no matter how
#: long we wait, and burning the full retry budget on one only delays the
#: operator's feedback while spending rate-limit headroom.
NON_RETRYABLE_STATUSES = frozenset({400, 401, 402, 403, 404, 405, 422})

#: HTTP statuses that mean the account itself cannot pay for the call. A 402
#: is *not* transient: waiting will not conjure credits. We must surface it
#: loudly (and let the orchestrator abort) rather than burning all retries
#: on a condition that can never change mid-build.
OUT_OF_CREDITS_STATUSES = frozenset({402})

#: Pulls a leading HTTP status out of SDK error text such as
#: "Error code: 401 - Invalid API key". Used only when the exception carries
#: no structured ``status_code``.
_STATUS_IN_TEXT_RE = re.compile(r"\b(?:error code|status(?:_code)?)\D{0,3}(\d{3})\b", re.I)


class LLMUnavailableError(RuntimeError):
    """Raised when the optional ``openai`` dependency is missing."""


class NonRetryableError(RuntimeError):
    """Wraps a provider error that retrying cannot possibly fix."""


class OutOfCreditsError(NonRetryableError):
    """OpenRouter returned 402 Payment Required for every attempt.

    The account cannot pay for the call, so retrying is pointless and the
    whole build must stop with a clear message rather than grinding through
    every remaining agent and cycle. Distinct from a generic non-retryable
    error so the orchestrator can abort fast and the CLI can exit with a
    dedicated code.
    """


def status_code_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction from an SDK exception."""
    for attribute in ("status_code", "status", "http_status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    match = _STATUS_IN_TEXT_RE.search(str(exc))
    if match:
        return int(match.group(1))
    return None


def is_retryable(exc: BaseException) -> bool:
    """False for client errors that will fail identically on every attempt."""
    status = status_code_of(exc)
    if status is None:
        return True  # unknown cause: assume transient (network blip, 5xx)
    if status == 429:
        return True  # rate limited: backing off is exactly the right move
    return status not in NON_RETRYABLE_STATUSES


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token accounting for one call, when the provider reports it."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    def estimated_cost_usd(self, price_per_1k: float) -> float:
        """Rough spend in USD for this usage at ``price_per_1k`` USD / 1K tokens."""
        return round(self.total_tokens / 1000.0 * price_per_1k, 6)


def usage_of(response: object) -> TokenUsage:
    """Read ``response.usage`` defensively; providers may omit it entirely."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    prompt = getattr(usage, "prompt_tokens", 0)
    completion = getattr(usage, "completion_tokens", 0)
    return TokenUsage(
        prompt_tokens=prompt if isinstance(prompt, int) else 0,
        completion_tokens=completion if isinstance(completion, int) else 0,
    )


@dataclass(frozen=True, slots=True)
class AgentReply:
    """The outcome of one agent call.

    ``ok=False`` is a first-class state, not a magic string. The old code
    returned ``"[ERROR ...]"`` and relied on ``startswith`` at seven call
    sites; one missed check silently scored a failed agent as success.
    """

    text: str
    ok: bool
    attempts: int
    error: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    timed_out: bool = False
    #: True when the provider returned 402 Payment Required (account has no
    #: credits). Carried explicitly so the orchestrator can abort the whole
    #: build instead of treating it as an ordinary per-agent failure.
    out_of_credits: bool = False

    @property
    def failed(self) -> bool:
        return not self.ok


class OpenRouterClient:
    """Thin async wrapper over the OpenAI SDK pointed at OpenRouter.

    Adds three things the raw SDK does not give us:

    * a **hard per-call timeout**, so one stalled connection cannot wedge a
      24/7 daemon (the subprocess had a timeout; this call did not);
    * **retry classification**, so a 401 fails fast instead of burning the
      whole budget on an error that can never succeed;
    * **token accounting**, so an unattended daemon reports what it spends.
    """

    def __init__(
        self,
        openrouter: OpenRouterConfig,
        retry: RetryPolicy,
        *,
        client: object | None = None,
        sdk: object | None = None,
        model_prices_usd_per_1k: Mapping[str, float] | None = None,
        default_token_price_usd: float = 0.002,
    ) -> None:
        self.config = openrouter
        self.retry = retry
        self.total_usage = TokenUsage()
        self.call_count = 0
        self._model_prices = dict(model_prices_usd_per_1k or {})
        self._default_price = default_token_price_usd
        #: Spend is accumulated per call, while the model is still known.
        #: Deriving it afterwards from ``total_usage`` is impossible: usage
        #: carries no model, so every token would be priced at the generic
        #: default and an Opus-heavy run under-reported by roughly 7x --
        #: making ``max_cost_usd`` (ADR-005) a budget in name only.
        self._cost_usd = 0.0
        self._cost_by_model: dict[str, float] = {}

        if client is not None:
            self._client = client
            return

        if sdk is None:  # pragma: no cover - exercised via the sdk= injection
            try:
                import openai

                sdk = openai
            except ImportError as exc:
                raise LLMUnavailableError(
                    "The 'openai' package is required to call OpenRouter. "
                    "Install it with: pip install openai"
                ) from exc

        if not openrouter.api_key:
            logger.error(
                "No OpenRouter API key in $%s - every agent call will fail. "
                "Export it: export %s=sk-or-...",
                openrouter.api_key_env,
                openrouter.api_key_env,
            )

        factory = getattr(sdk, "AsyncOpenAI")
        self._client = factory(
            base_url=openrouter.base_url,
            api_key=openrouter.api_key,
            default_headers=openrouter.default_headers() or None,
        )

    async def call(
        self,
        agent: AgentSpec,
        prompt: str,
        *,
        extra_system: str = "",
    ) -> AgentReply:
        """Call ``agent`` with ``prompt``, retrying only transient failures."""
        system_prompt = (
            f"You are the {agent.role} on an autonomous multi-agent software "
            "engineering team. Stay strictly within this role's responsibilities."
        )
        if extra_system:
            system_prompt = f"{system_prompt} {extra_system}"

        last_error: BaseException | None = None
        timed_out = False

        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                response = await asyncio.wait_for(
                    self._client.chat.completions.create(  # type: ignore[attr-defined]
                        model=agent.model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                        max_tokens=agent.max_tokens,
                        temperature=agent.temperature,
                    ),
                    timeout=self.config.request_timeout_seconds,
                )
                usage = usage_of(response)
                self.total_usage = self.total_usage + usage
                call_cost = usage.estimated_cost_usd(self.model_price_per_1k(agent.model))
                self._cost_usd += call_cost
                self._cost_by_model[agent.model] = round(
                    self._cost_by_model.get(agent.model, 0.0) + call_cost, 6
                )
                self.call_count += 1
                logger.info(
                    "Agent %s ok in %d attempt(s); tokens=%d (run total=%d) cost=$%.4f",
                    agent.role,
                    attempt,
                    usage.total_tokens,
                    self.total_usage.total_tokens,
                    call_cost,
                )
                return AgentReply(
                    text=response.choices[0].message.content or "",
                    ok=True,
                    attempts=attempt,
                    usage=usage,
                )
            except asyncio.CancelledError:
                # Must propagate: swallowing this makes the daemon unstoppable.
                raise
            except asyncio.TimeoutError as exc:
                timed_out = True
                last_error = exc
                logger.warning(
                    "Agent %s attempt %d/%d timed out after %.0fs",
                    agent.role,
                    attempt,
                    self.retry.max_attempts,
                    self.config.request_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - classify, retry, surface
                timed_out = False
                last_error = exc
                status = status_code_of(exc)
                if status in OUT_OF_CREDITS_STATUSES:
                    # The account cannot pay. Retrying cannot change this, and
                    # burning the remaining attempts only delays the operator's
                    # feedback. Fail fast so the build can stop early.
                    logger.error(
                        "Agent %s failed: OpenRouter returned 402 Payment Required "
                        "- the account has no credits. Add credits at "
                        "https://openrouter.ai/settings/credits (HTTP 402).",
                        agent.role,
                    )
                    return self._failure(agent, exc, attempt, timed_out=False, out_of_credits=True)
                if not is_retryable(exc):
                    logger.error(
                        "Agent %s failed with a non-retryable error (HTTP %s): %s",
                        agent.role,
                        status,
                        exc,
                    )
                    return self._failure(agent, exc, attempt, timed_out=False)
                logger.warning(
                    "Agent %s attempt %d/%d failed: %s",
                    agent.role,
                    attempt,
                    self.retry.max_attempts,
                    exc,
                )

            if attempt < self.retry.max_attempts:
                await asyncio.sleep(self.retry.delay_for(attempt))

        return self._failure(agent, last_error, self.retry.max_attempts, timed_out=timed_out)

    def _failure(
        self,
        agent: AgentSpec,
        error: BaseException | None,
        attempts: int,
        *,
        timed_out: bool,
        out_of_credits: bool = False,
    ) -> AgentReply:
        message = f"{ERROR_PREFIX} calling {agent.role} ({agent.model}): {error}]"
        return AgentReply(
            text=message,
            ok=False,
            attempts=attempts,
            error=str(error),
            timed_out=timed_out,
            out_of_credits=out_of_credits,
        )

    def model_price_per_1k(self, model: str) -> float:
        """USD price per 1K tokens for ``model``, or the configured default."""
        return float(self._model_prices.get(model, self._default_price))

    def running_cost_usd(self) -> float:
        """Estimated spend so far, priced per-model at the time of each call."""
        return round(self._cost_usd, 6)

    def cost_by_model(self) -> dict[str, float]:
        """Per-model spend breakdown -- what an operator needs from /metrics."""
        return dict(self._cost_by_model)
