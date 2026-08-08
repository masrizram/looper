"""OpenRouter LLM client: retries, backoff, and explicit failure signalling."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from looper.config import AgentSpec, OpenRouterConfig, RetryPolicy

logger = logging.getLogger("looper.llm")

#: Prefix marking a failed agent call. Callers MUST check ``AgentReply.ok``
#: rather than pattern-matching this string.
ERROR_PREFIX = "[ERROR"


class LLMUnavailableError(RuntimeError):
    """Raised when the optional ``openai`` dependency is missing."""


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

    @property
    def failed(self) -> bool:
        return not self.ok


class OpenRouterClient:
    """Thin async wrapper over the OpenAI SDK pointed at OpenRouter."""

    def __init__(
        self,
        openrouter: OpenRouterConfig,
        retry: RetryPolicy,
        *,
        client: object | None = None,
        sdk: object | None = None,
    ) -> None:
        self.config = openrouter
        self.retry = retry

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
        """Call ``agent`` with ``prompt``, retrying transient failures."""
        system_prompt = (
            f"You are the {agent.role} on an autonomous multi-agent software "
            "engineering team. Stay strictly within this role's responsibilities."
        )
        if extra_system:
            system_prompt = f"{system_prompt} {extra_system}"

        last_error: BaseException | None = None
        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                response = await self._client.chat.completions.create(  # type: ignore[attr-defined]
                    model=agent.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=agent.max_tokens,
                    temperature=agent.temperature,
                )
                return AgentReply(
                    text=response.choices[0].message.content or "",
                    ok=True,
                    attempts=attempt,
                )
            except asyncio.CancelledError:
                # Must propagate: swallowing this makes the daemon unstoppable.
                raise
            except Exception as exc:  # noqa: BLE001 - retry, then surface
                last_error = exc
                logger.warning(
                    "Agent %s attempt %d/%d failed: %s",
                    agent.role,
                    attempt,
                    self.retry.max_attempts,
                    exc,
                )
                if attempt < self.retry.max_attempts:
                    await asyncio.sleep(self.retry.delay_for(attempt))

        message = f"{ERROR_PREFIX} calling {agent.role} ({agent.model}): {last_error}]"
        return AgentReply(
            text=message,
            ok=False,
            attempts=self.retry.max_attempts,
            error=str(last_error),
        )
