"""HTTP control plane: /build, /status, /health, /metrics."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Deque

from looper.config import HTTPConfig

logger = logging.getLogger("looper.server")

GoalCallback = Callable[[str], Awaitable[Any]]
StatusProvider = Callable[[], dict[str, Any]]


class HTTPUnavailableError(RuntimeError):
    """Raised when the optional ``aiohttp`` dependency is missing."""


class RateLimiter:
    """Fixed-window-per-client limiter.

    Keeps a bounded deque of hit timestamps per client and prunes on read, so
    idle clients cost nothing and memory cannot grow without bound.
    """

    def __init__(self, limit_per_minute: int, window_seconds: float = 60.0) -> None:
        self.limit = limit_per_minute
        self.window = window_seconds
        self._hits: dict[str, Deque[float]] = {}

    def allow(self, client: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        bucket = self._hits.setdefault(client, deque())
        cutoff = current - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            # Drop empty buckets so a scan of many client IPs cannot leak memory.
            self._hits.pop(client, None)
            bucket = self._hits.setdefault(client, deque())
        if len(bucket) >= self.limit:
            return False
        bucket.append(current)
        return True

    def reset(self) -> None:
        self._hits.clear()


class HTTPServer:
    """aiohttp application exposing the daemon's control endpoints."""

    def __init__(
        self,
        config: HTTPConfig,
        callback: GoalCallback,
        *,
        status_provider: StatusProvider | None = None,
        web_module: Any | None = None,
    ) -> None:
        self.config = config
        self.callback = callback
        self.status_provider = status_provider or (lambda: {})
        self.limiter = RateLimiter(config.rate_limit_per_minute)
        self._runner: Any | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._started_at = time.monotonic()
        self._counters = {"build_accepted": 0, "build_rejected": 0, "build_unauthorized": 0}

        if web_module is not None:
            self._web = web_module
        else:
            try:
                from aiohttp import web as web_module_import
            except ImportError as exc:  # pragma: no cover - env-specific
                raise HTTPUnavailableError(
                    "The 'aiohttp' package is required for the HTTP server. "
                    "Install it with: pip install aiohttp"
                ) from exc
            self._web = web_module_import

    # -- Lifecycle -------------------------------------------------------

    def build_app(self) -> Any:
        app = self._web.Application(client_max_size=self.config.max_body_bytes)
        app.add_routes(
            [
                self._web.post("/build", self.handle_build),
                self._web.get("/status", self.handle_status),
                self._web.get("/health", self.handle_health),
                self._web.get("/metrics", self.handle_metrics),
            ]
        )
        return app

    async def start(self) -> None:
        app = self.build_app()
        runner = self._web.AppRunner(app)
        await runner.setup()
        site = self._web.TCPSite(runner, self.config.bind, self.config.port)
        await site.start()
        self._runner = runner
        logger.info("HTTP server listening on %s:%s", self.config.bind, self.config.port)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- Helpers ---------------------------------------------------------

    def _authorized(self, request: Any) -> bool:
        if not self.config.auth_token:
            return True
        header = request.headers.get("Authorization", "")
        # Constant-time: a plain != leaks the token byte-by-byte to anyone who
        # can measure response latency.
        return hmac.compare_digest(header, f"Bearer {self.config.auth_token}")

    @staticmethod
    def _client_id(request: Any) -> str:
        remote = getattr(request, "remote", None)
        return str(remote) if remote else "unknown"

    def _track(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        # Without this the set grows forever - a slow memory leak in a daemon.
        task.add_done_callback(self._tasks.discard)
        # A background build that raises used to vanish into asyncio's
        # "Task exception was never retrieved" noise: the operator got a 200
        # and no signal at all that the run had died.
        task.add_done_callback(self._log_task_failure)
        return task

    @staticmethod
    def _log_task_failure(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Background build failed: %s", exc, exc_info=exc)

    def _json(self, payload: dict[str, Any], status: int = 200) -> Any:
        return self._web.json_response(payload, status=status)

    # -- Handlers --------------------------------------------------------

    async def handle_build(self, request: Any) -> Any:
        if not self._authorized(request):
            self._counters["build_unauthorized"] += 1
            return self._json({"error": "unauthorized"}, status=401)

        if not self.limiter.allow(self._client_id(request)):
            self._counters["build_rejected"] += 1
            return self._json({"error": "rate limit exceeded"}, status=429)

        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            self._counters["build_rejected"] += 1
            return self._json({"error": "invalid JSON body"}, status=400)
        except Exception:  # noqa: BLE001 - aiohttp raises its own body errors
            self._counters["build_rejected"] += 1
            return self._json({"error": "invalid or oversized body"}, status=400)

        if not isinstance(data, dict):
            self._counters["build_rejected"] += 1
            return self._json({"error": "body must be a JSON object"}, status=400)

        goal_raw = data.get("goal")
        if not isinstance(goal_raw, str) or not goal_raw.strip():
            self._counters["build_rejected"] += 1
            return self._json({"error": "goal required"}, status=400)

        goal = goal_raw.strip()
        if len(goal) > self.config.max_goal_length:
            self._counters["build_rejected"] += 1
            return self._json(
                {"error": f"goal too long (max {self.config.max_goal_length} chars)"},
                status=413,
            )

        self._track(self.callback(goal))
        self._counters["build_accepted"] += 1
        return self._json({"status": "started", "goal": goal})

    async def handle_status(self, request: Any) -> Any:
        if not self._authorized(request):
            return self._json({"error": "unauthorized"}, status=401)
        return self._json(self.status_provider())

    async def handle_health(self, request: Any) -> Any:
        """Unauthenticated liveness probe. Exposes no build data."""
        del request
        return self._json({"status": "ok", "uptime_seconds": round(self.uptime, 3)})

    async def handle_metrics(self, request: Any) -> Any:
        if not self._authorized(request):
            return self._json({"error": "unauthorized"}, status=401)
        return self._json(
            {
                "uptime_seconds": round(self.uptime, 3),
                "in_flight_builds": len(self._tasks),
                **self._counters,
            }
        )

    @property
    def uptime(self) -> float:
        return time.monotonic() - self._started_at
