"""Shared fixtures and fakes.

No test in this suite touches the network, the real environment, or the real
clock. Everything the daemon depends on is injected.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from looper.config import LooperConfig, build_config
from looper.llm import OpenRouterClient
from looper.orchestrator import LooperDaemon
from looper.phases import PhaseManager
from looper.state import StateManager


@dataclass
class FakeMessage:
    content: str


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeResponse:
    choices: list[FakeChoice]


class FakeCompletions:
    """Stands in for ``client.chat.completions``.

    Replies are keyed by the agent's **role string** (which appears in the
    system prompt), not by model name: several agents share one model, so a
    model->reply map would be ambiguous.
    """

    def __init__(self, replies: dict[str, str], fail_with: Exception | None = None) -> None:
        self.replies = replies
        self.fail_with = fail_with
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        system_text = kwargs["messages"][0]["content"]
        for role, reply in self.replies.items():
            if role in system_text:
                return FakeResponse([FakeChoice(FakeMessage(reply))])
        return FakeResponse([FakeChoice(FakeMessage("ok"))])


class FakeChat:
    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions


class FakeSDKClient:
    def __init__(self, replies: dict[str, str], fail_with: Exception | None = None) -> None:
        self.completions = FakeCompletions(replies, fail_with)
        self.chat = FakeChat(self.completions)


DEFAULT_REPLIES = {
    "Senior Technical Researcher": "research notes",
    "System Architect": "architecture notes",
    "UX/API Designer": "api notes",
    "Code Builder": "print('hello')",
    "Test Generator": "def test_x(): assert True",
    "Senior Reviewer": "Looks fine.\nScore: 97",
    "Security Auditor": "No issues found.",
    "Performance Optimizer": "optimized",
    "Documentation Writer": "# README",
    "Expert Fixer": "print('fixed')",
}


@pytest.fixture
def raw_config(tmp_path) -> dict[str, Any]:
    return {
        "workspace": str(tmp_path / "workspace"),
        "state_file": str(tmp_path / "state.json"),
        "watch_file": str(tmp_path / "commands.txt"),
        "http": {"bind": "127.0.0.1", "port": 8999},
        "execution": {"max_cycles": 2, "target_score": 99, "min_acceptable": 60},
        "retry": {"max_attempts": 2, "backoff_base": 1.0, "backoff_max": 0.0},
    }


@pytest.fixture
def config(raw_config) -> LooperConfig:
    return build_config(raw_config, env={})


@pytest.fixture
def state(config) -> StateManager:
    return StateManager(config.state_file, config.execution.max_history_entries)


def make_client(config: LooperConfig, replies=None, fail_with=None) -> OpenRouterClient:
    sdk_client = FakeSDKClient(replies if replies is not None else DEFAULT_REPLIES, fail_with)
    return OpenRouterClient(config.openrouter, config.retry, client=sdk_client)


@pytest.fixture
def client(config) -> OpenRouterClient:
    return make_client(config)


@pytest.fixture
def phases(config, state, client) -> PhaseManager:
    return PhaseManager(config, state, client)


class FakeWebResponse:
    def __init__(self, payload: dict[str, Any], status: int) -> None:
        self.payload = payload
        self.status = status


class FakeApp:
    """Dict-like aiohttp Application stand-in with add_routes()."""

    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.routes: list[Any] = []
        self.kwargs = kwargs
        self._store: dict[str, Any] = {}

    def add_routes(self, routes) -> None:
        self.routes.extend(routes)

    def __getitem__(self, key: str) -> Any:
        if key == "routes":
            return self.routes
        if key == "kwargs":
            return self.kwargs
        return self._store[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._store[key] = value


class FakeWebModule:
    """Minimal stand-in for ``aiohttp.web`` so server tests need no sockets."""

    def __init__(self) -> None:
        self.routes: list[Any] = []
        self.started = False
        self.cleaned = False

    def json_response(self, payload, status: int = 200) -> FakeWebResponse:
        return FakeWebResponse(payload, status)

    def Application(self, **kwargs: Any) -> "FakeApp":  # noqa: N802
        return FakeApp(kwargs)

    def post(self, path: str, handler) -> tuple[str, str, Any]:
        return ("POST", path, handler)

    def get(self, path: str, handler) -> tuple[str, str, Any]:
        return ("GET", path, handler)

    def AppRunner(self, app):  # noqa: N802
        module = self

        class Runner:
            def __init__(self) -> None:
                self.app = app

            async def setup(self) -> None:
                module.started = True

            async def cleanup(self) -> None:
                module.cleaned = True

        return Runner()

    def TCPSite(self, runner, bind, port):  # noqa: N802
        class Site:
            async def start(self) -> None:
                return None

        return Site()


class FakeRequest:
    """aiohttp-like request stub."""

    def __init__(
        self,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        remote: str = "1.2.3.4",
        raises: Exception | None = None,
    ) -> None:
        self._payload = payload
        self.headers = headers or {}
        self.remote = remote
        self._raises = raises

    async def json(self) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._payload


@pytest.fixture
def daemon_factory(config):
    """Builds a fully-wired LooperDaemon with fakes for every boundary."""

    def _factory(
        replies=None,
        fail_with=None,
        cfg: LooperConfig | None = None,
        server=None,
        watcher=None,
    ) -> LooperDaemon:
        effective = cfg or config
        state = StateManager(effective.state_file, effective.execution.max_history_entries)
        llm = make_client(effective, replies, fail_with)
        phase_manager = PhaseManager(effective, state, llm)
        from looper.server import HTTPServer

        http = server or HTTPServer(
            effective.http, lambda goal: asyncio.sleep(0), web_module=FakeWebModule()
        )
        return LooperDaemon(
            effective,
            state=state,
            client=llm,
            phases=phase_manager,
            server=http,
            watcher=watcher,
        )

    return _factory


@pytest.fixture
def stub_pytest_run(monkeypatch):
    """Stub the generated-test subprocess.

    Spawning a real pytest inside pytest returns RC 4 ("no tests ran") because
    the parent's output capture interferes, so we never do it.
    """

    def _stub(
        stdout: str = "1 passed in 0.01s", stderr: str = "", returncode: int = 0, raises=None
    ):
        class Proc:
            pass

        proc = Proc()
        proc.stdout = stdout
        proc.stderr = stderr
        proc.returncode = returncode

        async def fake_to_thread(func, *args, **kwargs):
            if raises is not None:
                raise raises
            return proc

        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
        return proc

    return _stub
