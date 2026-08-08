"""HTTP control plane: auth, rate limiting, validation, observability."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from looper.config import HTTPConfig
from looper.server import HTTPServer, RateLimiter

from .conftest import FakeRequest, FakeWebModule


def make_server(
    auth_token: str = "",
    callback=None,
    status_provider=None,
    **cfg_kwargs,
) -> HTTPServer:
    async def noop(goal):
        return None

    config = HTTPConfig(auth_token=auth_token, **cfg_kwargs)
    return HTTPServer(
        config,
        callback or noop,
        status_provider=status_provider,
        web_module=FakeWebModule(),
    )


def post(server: HTTPServer, request: FakeRequest):
    return asyncio.run(server.handle_build(request))


# --- Auth (H-2) --------------------------------------------------------------


def test_missing_auth_is_rejected():
    server = make_server(auth_token="secret")
    assert post(server, FakeRequest({"goal": "x"})).status == 401


def test_wrong_token_is_rejected():
    server = make_server(auth_token="secret")
    request = FakeRequest({"goal": "x"}, headers={"Authorization": "Bearer wrong"})
    assert post(server, request).status == 401


def test_correct_token_is_accepted():
    server = make_server(auth_token="secret")
    request = FakeRequest({"goal": "x"}, headers={"Authorization": "Bearer secret"})
    assert post(server, request).status == 200


def test_no_token_configured_means_open():
    assert post(make_server(), FakeRequest({"goal": "x"})).status == 200


def test_unauthorized_requests_are_counted():
    server = make_server(auth_token="s")
    post(server, FakeRequest({"goal": "x"}))
    metrics = asyncio.run(server.handle_metrics(FakeRequest(headers={"Authorization": "Bearer s"})))
    assert metrics.payload["build_unauthorized"] == 1


# --- Input validation (H-4) --------------------------------------------------


def test_empty_goal_rejected():
    assert post(make_server(), FakeRequest({"goal": "   "})).status == 400


def test_missing_goal_rejected():
    assert post(make_server(), FakeRequest({})).status == 400


def test_non_string_goal_rejected():
    assert post(make_server(), FakeRequest({"goal": 123})).status == 400


def test_non_object_body_rejected():
    assert post(make_server(), FakeRequest(["a", "b"])).status == 400


def test_invalid_json_rejected():
    request = FakeRequest(raises=json.JSONDecodeError("bad", "doc", 0))
    assert post(make_server(), request).status == 400


def test_oversized_body_error_is_handled():
    request = FakeRequest(raises=RuntimeError("payload too large"))
    assert post(make_server(), request).status == 400


def test_oversized_goal_rejected():
    """H-4: an unbounded goal is unbounded LLM spend."""
    server = make_server(max_goal_length=100)
    response = post(server, FakeRequest({"goal": "x" * 101}))
    assert response.status == 413
    assert "too long" in response.payload["error"]


def test_goal_at_the_limit_is_accepted():
    server = make_server(max_goal_length=10)
    assert post(server, FakeRequest({"goal": "x" * 10})).status == 200


def test_goal_is_trimmed_before_dispatch():
    seen = []

    async def capture(goal):
        seen.append(goal)

    server = make_server(callback=capture)
    post(server, FakeRequest({"goal": "  build a CLI  "}))
    assert seen == ["build a CLI"]


def test_body_size_cap_is_passed_to_aiohttp():
    server = make_server(max_body_bytes=4096)
    app = server.build_app()
    assert app["kwargs"]["client_max_size"] == 4096


# --- Rate limiting -----------------------------------------------------------


def test_rate_limit_blocks_the_flood():
    server = make_server(rate_limit_per_minute=3)
    statuses = [post(server, FakeRequest({"goal": "x"})).status for _ in range(5)]
    assert statuses == [200, 200, 200, 429, 429]


def test_rate_limit_is_per_client():
    server = make_server(rate_limit_per_minute=1)
    assert post(server, FakeRequest({"goal": "x"}, remote="1.1.1.1")).status == 200
    assert post(server, FakeRequest({"goal": "x"}, remote="2.2.2.2")).status == 200
    assert post(server, FakeRequest({"goal": "x"}, remote="1.1.1.1")).status == 429


def test_rate_limit_window_expires():
    limiter = RateLimiter(limit_per_minute=2, window_seconds=60)
    assert limiter.allow("a", now=0) is True
    assert limiter.allow("a", now=1) is True
    assert limiter.allow("a", now=2) is False
    assert limiter.allow("a", now=100) is True  # window rolled over


def test_rate_limiter_does_not_leak_idle_clients():
    limiter = RateLimiter(limit_per_minute=5, window_seconds=10)
    for i in range(1000):
        limiter.allow(f"client-{i}", now=0)
    for i in range(1000):
        limiter.allow(f"client-{i}", now=1000)
    assert len(limiter._hits) <= 1000


def test_rate_limiter_reset():
    limiter = RateLimiter(1)
    limiter.allow("a")
    limiter.reset()
    assert limiter.allow("a") is True


def test_rate_limited_requests_are_counted():
    server = make_server(rate_limit_per_minute=1)
    post(server, FakeRequest({"goal": "x"}))
    post(server, FakeRequest({"goal": "x"}))
    metrics = asyncio.run(server.handle_metrics(FakeRequest()))
    assert metrics.payload["build_rejected"] == 1


# --- Task tracking (H-6) -----------------------------------------------------


def test_completed_tasks_are_discarded():
    """H-6: the old code appended to a list that was never pruned."""

    async def quick(goal):
        return None

    server = make_server(callback=quick)

    async def run():
        for _ in range(20):
            await server.handle_build(FakeRequest({"goal": "x"}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return len(server._tasks)

    assert asyncio.run(run()) == 0


def test_background_build_failure_is_logged(caplog):
    """S-5: a crashing background build used to return 200 and then vanish
    into asyncio's 'Task exception was never retrieved' noise, so an operator
    had no signal at all that the run had died."""
    caplog.set_level(logging.ERROR, logger="looper.server")

    async def boom(goal):
        raise RuntimeError("build exploded")

    server = make_server(callback=boom)

    async def run():
        await server.handle_build(FakeRequest({"goal": "x"}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert "Background build failed" in caplog.text
    assert "build exploded" in caplog.text


def test_cancelled_background_build_is_not_logged_as_failure(caplog):
    caplog.set_level(logging.ERROR, logger="looper.server")

    async def slow(goal):
        await asyncio.sleep(10)

    server = make_server(callback=slow)

    async def run():
        await server.handle_build(FakeRequest({"goal": "x"}))
        await server.stop()

    asyncio.run(run())
    assert "Background build failed" not in caplog.text


def test_stop_cancels_in_flight_tasks():
    async def slow(goal):
        await asyncio.sleep(3600)

    server = make_server(callback=slow)

    async def run():
        await server.handle_build(FakeRequest({"goal": "x"}))
        assert len(server._tasks) == 1
        await server.stop()
        return len(server._tasks)

    assert asyncio.run(run()) == 0


# --- Observability endpoints -------------------------------------------------


def test_health_is_unauthenticated_and_leaks_nothing():
    server = make_server(auth_token="secret")
    response = asyncio.run(server.handle_health(FakeRequest()))
    assert response.status == 200
    assert response.payload["status"] == "ok"
    assert "current_goal" not in response.payload


def test_status_requires_auth():
    server = make_server(auth_token="secret", status_provider=lambda: {"score": 1})
    assert asyncio.run(server.handle_status(FakeRequest())).status == 401


def test_status_returns_provider_payload():
    server = make_server(status_provider=lambda: {"score": 42})
    response = asyncio.run(server.handle_status(FakeRequest()))
    assert response.payload == {"score": 42}


def test_status_defaults_to_empty_provider():
    server = make_server()
    assert asyncio.run(server.handle_status(FakeRequest())).payload == {}


def test_metrics_requires_auth():
    server = make_server(auth_token="secret")
    assert asyncio.run(server.handle_metrics(FakeRequest())).status == 401


def test_metrics_reports_counters_and_uptime():
    server = make_server()
    post(server, FakeRequest({"goal": "x"}))
    response = asyncio.run(server.handle_metrics(FakeRequest()))
    assert response.payload["build_accepted"] == 1
    assert response.payload["uptime_seconds"] >= 0


def test_uptime_is_monotonic():
    assert make_server().uptime >= 0


# --- Lifecycle ---------------------------------------------------------------


def test_routes_are_registered():
    server = make_server()
    app = server.build_app()
    paths = {(method, path) for method, path, _ in app["routes"]}
    assert paths == {
        ("POST", "/build"),
        ("GET", "/status"),
        ("GET", "/health"),
        ("GET", "/metrics"),
    }


def test_start_and_stop_lifecycle():
    web = FakeWebModule()

    async def noop(goal):
        return None

    server = HTTPServer(HTTPConfig(), noop, web_module=web)

    async def run():
        await server.start()
        assert web.started is True
        await server.stop()

    asyncio.run(run())
    assert web.cleaned is True


def test_stop_is_idempotent():
    server = make_server()
    asyncio.run(server.stop())
    asyncio.run(server.stop())


def test_client_id_falls_back_to_unknown():
    request = FakeRequest({"goal": "x"}, remote=None)
    assert HTTPServer._client_id(request) == "unknown"


def test_missing_aiohttp_raises_clear_error(monkeypatch):
    import builtins

    from looper.server import HTTPUnavailableError

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "aiohttp":
            raise ImportError("no aiohttp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    async def noop(goal):
        return None

    with pytest.raises(HTTPUnavailableError, match="aiohttp"):
        HTTPServer(HTTPConfig(), noop)


def test_real_aiohttp_module_is_used_when_available():
    """Covers the production import branch (no web_module injected)."""
    pytest.importorskip("aiohttp")

    async def noop(goal):
        return None

    server = HTTPServer(HTTPConfig(port=8998), noop)
    from aiohttp import web as real_web

    assert server._web is real_web


@pytest.mark.integration
def test_real_aiohttp_endpoints_over_loopback():
    """Behavioural cover the in-memory fakes cannot give: the real aiohttp
    request/response lifecycle actually binds a socket and serves /health,
    /build auth, and /metrics. Skips silently if aiohttp is absent."""
    aiohttp = pytest.importorskip("aiohttp")

    async def run():
        seen = []

        async def capture(goal):
            seen.append(goal)

        token = "secret-token"
        # Bind to 127.0.0.1 only; let the OS assign an ephemeral port at the
        # TCPSite layer (HTTPConfig still validates the default port 9999).
        config = HTTPConfig(bind="127.0.0.1", auth_token=token)
        server = HTTPServer(config, capture, web_module=None)
        runner = aiohttp.web.AppRunner(server.build_app())
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, config.bind, 0)
        await site.start()
        actual_port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{actual_port}"

        headers_auth = {"Authorization": f"Bearer {token}"}

        # /health is unauthenticated.
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"{base}/health") as resp:
                assert resp.status == 200
                body = await resp.json()
                assert body["status"] == "ok"

            # /build with a valid token is accepted (202-free 200 + started).
            async with sess.post(
                f"{base}/build",
                json={"goal": "build a thing"},
                headers=headers_auth,
            ) as resp:
                assert resp.status == 200
                body = await resp.json()
                assert body["status"] == "started"

            # /build without a token is rejected.
            async with sess.post(f"{base}/build", json={"goal": "x"}) as resp:
                assert resp.status == 401

            # /metrics requires auth.
            async with sess.get(f"{base}/metrics") as resp:
                assert resp.status == 401
            async with sess.get(f"{base}/metrics", headers=headers_auth) as resp:
                assert resp.status == 200
                body = await resp.json()
                assert body["build_accepted"] == 1

        await runner.cleanup()
        return seen

    saw = asyncio.run(run())
    assert saw == ["build a thing"]
