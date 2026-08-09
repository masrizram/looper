"""Outbound webhook notifications for terminal build outcomes.

An unattended ``--daemon`` had exactly one way to tell an operator what
happened: they had to poll ``/status``. A build that ran out of credits at
03:00 was indistinguishable from one still running until somebody looked.
This module closes that gap with a deliberately small contract.

Design rules, all of which are load-bearing:

* **Notification failure never fails a build.** Every error is caught and
  logged. A flaky Slack endpoint must not turn a passing build into a red one
  -- the notifier is an observer, not a gate.
* **Fire only on terminal states.** Per-phase chatter would make the channel
  unreadable and is what ``/status`` is for.
* **No secrets in the payload.** The goal, score, status and cost go out;
  the API key, the auth token and the generated source never do.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from looper.config import NotificationsConfig

logger = logging.getLogger("looper.notify")

#: Signature of the transport, injectable so tests never touch the network.
Sender = Callable[[str, bytes, Mapping[str, str], float], int]

#: Terminal build states a notification may describe. Anything else is an
#: in-flight state and is not a notifiable event.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"passed", "below_minimum", "cost_exhausted", "out_of_credits", "failed"}
)


def _default_sender(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
    request = urllib.request.Request(  # nosec B310 - scheme validated in config
        url, data=body, headers=dict(headers), method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        status: int = int(response.status)
        return status


class Notifier:
    """Posts one JSON payload per terminal build outcome.

    ``config.webhook_url`` empty == feature off, and ``notify()`` becomes a
    no-op that costs one attribute read. That keeps the orchestrator free of
    ``if notifications_enabled`` branches.
    """

    def __init__(self, config: NotificationsConfig, *, sender: Sender | None = None) -> None:
        self.config = config
        self._send = sender or _default_sender

    @property
    def enabled(self) -> bool:
        return bool(self.config.webhook_url)

    def payload(
        self,
        *,
        status: str,
        goal: str,
        score: float,
        cycle: int,
        cost_usd: float,
        detail: str = "",
    ) -> dict[str, Any]:
        """Build the JSON body.

        ``text`` is included alongside the structured fields because Slack,
        Discord and Mattermost all render a bare ``text`` key without any
        receiver-side templating, so the default config works with each of
        them unmodified.
        """
        summary = (
            f"[looper] {status}: score {score:.2f} after {cycle} cycle(s), "
            f"${cost_usd:.4f} spent - {goal[:120]}"
        )
        if detail:
            summary = f"{summary} | {detail}"
        return {
            "text": summary,
            "status": status,
            "goal": goal,
            "score": round(score, 2),
            "cycle": cycle,
            "cost_usd": round(cost_usd, 6),
            "detail": detail,
        }

    def notify(
        self,
        *,
        status: str,
        goal: str,
        score: float,
        cycle: int,
        cost_usd: float,
        detail: str = "",
    ) -> bool:
        """Post the outcome. Returns True only when the endpoint accepted it.

        Never raises: a notification is best-effort telemetry layered over a
        build that has already finished.
        """
        if not self.enabled:
            return False
        if status not in TERMINAL_STATUSES:
            logger.debug("Not notifying for non-terminal status %r", status)
            return False
        if status not in self.config.on_status:
            logger.debug("Status %r not in notifications.on_status; skipping", status)
            return False

        body = json.dumps(
            self.payload(
                status=status,
                goal=goal,
                score=score,
                cycle=cycle,
                cost_usd=cost_usd,
                detail=detail,
            )
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", **dict(self.config.headers)}
        try:
            code = self._send(self.config.webhook_url, body, headers, self.config.timeout_seconds)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Deliberately broad-ish: DNS failure, TLS failure, refused
            # connection, and a malformed URL all mean "could not notify",
            # and none of them may propagate into the build result.
            logger.warning("Webhook notification failed: %s", exc)
            return False
        if 200 <= code < 300:
            logger.info("Webhook notified (%s): %s", code, status)
            return True
        logger.warning("Webhook endpoint returned HTTP %s for status %r", code, status)
        return False
