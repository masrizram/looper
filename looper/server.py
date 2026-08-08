"""HTTP control plane: /build, /status, /health, /metrics."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any, Deque

from looper.config import HTTPConfig

logger = logging.getLogger("looper.server")

GoalCallback = Callable[[str], Awaitable[Any]]
StatusProvider = Callable[[], dict[str, Any]]


class HTTPUnavailableError(RuntimeError):
    """Raised when the optional ``aiohttp`` dependency is missing."""


class RateLimiter:
    """Fixed-window-per-client limiter with a bounded client table.

    Keeps a deque of hit timestamps per client and prunes on read. The client
    table itself is capped: an earlier version popped an empty bucket and
    immediately recreated it via ``setdefault``, a no-op that left one dict
    entry per IP forever. A scan of many source addresses is exactly the
    traffic a public daemon sees, so the table is now evicted oldest-first.
    """

    #: Hard ceiling on tracked clients. Well above any legitimate fan-in.
    MAX_CLIENTS = 4096

    def __init__(self, limit_per_minute: int, window_seconds: float = 60.0) -> None:
        self.limit = limit_per_minute
        self.window = window_seconds
        self._hits: OrderedDict[str, Deque[float]] = OrderedDict()

    def _evict(self, cutoff: float) -> None:
        """Drop clients whose most recent hit fell out of the window."""
        for client in [c for c, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]:
            del self._hits[client]

    def allow(self, client: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window
        self._evict(cutoff)
        bucket = self._hits.get(client)
        if bucket is None:
            bucket = deque()
            self._hits[client] = bucket
        self._hits.move_to_end(client)
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        # Enforce the ceiling *after* inserting: evicting first let the table
        # settle one entry above MAX_CLIENTS on every call.
        while len(self._hits) > self.MAX_CLIENTS:
            self._hits.popitem(last=False)
        if len(bucket) >= self.limit:
            return False
        bucket.append(current)
        return True

    @property
    def tracked_clients(self) -> int:
        """Size of the client table - asserted by the memory-growth test."""
        return len(self._hits)

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

    def _client_id(self, request: Any) -> str:
        """Identify the rate-limit bucket for ``request``.

        ``X-Forwarded-For`` is honoured only when the immediate peer is a
        configured trusted proxy. Believing it unconditionally would let any
        caller forge a fresh identity per request and bypass the limit;
        ignoring it entirely behind a real proxy would collapse every client
        into one bucket. Both failure modes are worse than this check.
        """
        remote = getattr(request, "remote", None)
        peer = str(remote) if remote else "unknown"
        if peer in self.config.trusted_proxies:
            headers = getattr(request, "headers", {})
            forwarded = headers.get("X-Forwarded-For", "") if headers else ""
            if forwarded:
                # Left-most entry is the original client per RFC 7239 usage.
                return forwarded.split(",")[0].strip() or peer
        return peer

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
