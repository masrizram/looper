"""Unit & integration tests for Looper Daemon.

Run with:  pytest
These tests never touch the network/OpenRouter (the AsyncOpenAI client is
patched), so they are safe in CI without credentials.
"""

import asyncio
from unittest import mock

import pytest

import daemon

# ---------------------------------------------------------------------------
# Configuration (B-1: filename fallback + validation)
# ---------------------------------------------------------------------------


def test_load_config_reads_config_yaml(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("workspace: ./w\nhttp_port: 8765\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = daemon.load_config()
    assert cfg["http_port"] == 8765


def test_load_config_falls_back_to_looper_config_yaml(tmp_path, monkeypatch):
    (tmp_path / "looper_config.yaml").write_text(
        "workspace: ./w\nhttp_port: 8766\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    cfg = daemon.load_config()
    assert cfg["http_port"] == 8766


def test_load_config_raises_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        daemon.load_config()


def test_validate_config_rejects_bad_port():
    with pytest.raises(ValueError):
        daemon.validate_config({"http_port": "not-a-number"})


def test_validate_config_rejects_bad_max_cycles():
    with pytest.raises(ValueError):
        daemon.validate_config({"execution": {"max_cycles": 0}})


def test_validate_config_rejects_bad_score_order():
    with pytest.raises(ValueError):
        daemon.validate_config({"execution": {"min_acceptable": 99, "target_score": 50}})


def test_configure_picks_up_http_section():
    raw = {
        "http": {"bind": "127.0.0.1", "port": 8123, "auth_token_env": "X"},
        "execution": {"max_cycles": 3},
    }
    daemon.configure(raw)
    assert daemon.HTTP_PORT == 8123
    assert daemon.HTTP_BIND == "127.0.0.1"
    assert daemon.MAX_CYCLES == 3


# ---------------------------------------------------------------------------
# Test summary parser (B-2: robust pytest parsing)
# ---------------------------------------------------------------------------


def test_parse_test_summary_counts_from_summary_line():
    out = (
        "tests/test_gen.py::test_foo PASSED [ 50%]\n"
        "tests/test_gen.py::test_bar PASSED [100%]\n"
        "2 passed in 0.01s"
    )
    passed, failed = daemon.parse_test_summary(out)
    assert (passed, failed) == (2, 0)


def test_parse_test_summary_handles_failed():
    out = "tests/test_gen.py::test_x FAILED [100%]\n1 failed in 0.01s"
    passed, failed = daemon.parse_test_summary(out)
    assert (passed, failed) == (0, 1)


def test_parse_test_summary_no_double_count_on_name_with_passed():
    # Edge case: a test literally named test_passed_flag must not inflate count.
    out = "tests/test_gen.py::test_passed_flag FAILED [100%]\n1 failed in 0.01s"
    passed, failed = daemon.parse_test_summary(out)
    assert (passed, failed) == (0, 1)


def test_parse_test_summary_mixed():
    out = "3 passed, 2 failed in 0.02s"
    assert daemon.parse_test_summary(out) == (3, 2)


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------


def test_scoring_full():
    s = daemon.ScoringEngine.calculate_score(True, 10, 10, [], 100)
    assert s == 100.0


def test_scoring_no_build():
    s = daemon.ScoringEngine.calculate_score(False, 10, 10, [], 100)
    assert s == 80.0  # no +20 build bonus


def test_scoring_security_penalty():
    s = daemon.ScoringEngine.calculate_score(True, 10, 10, ["HIGH: x", "LOW: y"], 100)
    # 30 - 2*5 = 20 security -> 20+20+30+20 = 90
    assert s == 90.0


# ---------------------------------------------------------------------------
# Security / review false-negative guard (B-3, B-4)
# ---------------------------------------------------------------------------


def test_security_audit_failure_is_blocking():
    dm = _make_daemon()
    result = asyncio.run(dm.phases.run_security_audit("goal"))
    assert result["security_issues"] == ["CRITICAL: security audit did not complete"]


def test_review_failure_scores_zero():
    dm = _make_daemon()
    result = asyncio.run(dm.phases.run_review("goal"))
    assert result["review_score"] == 0.0


# ---------------------------------------------------------------------------
# State manager (P2: batch write, atomic save, default merge)
# ---------------------------------------------------------------------------


def test_state_reset_roundtrip(tmp_path):
    sm = daemon.StateManager(tmp_path / "state.json")
    sm.reset()
    assert sm.state["status"] == "idle"
    assert tmp_path.joinpath("state.json").exists()
    reloaded = daemon.StateManager(tmp_path / "state.json")
    assert reloaded.state["status"] == "idle"


def test_state_corrupt_recovers(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not valid json", encoding="utf-8")
    sm = daemon.StateManager(p)
    assert sm.state["status"] == "idle"


# ---------------------------------------------------------------------------
# HTTP auth (K-1 / P1)
# ---------------------------------------------------------------------------


def test_http_requires_auth_when_token_set():
    async def cb(goal):
        return None

    srv = daemon.HTTPServer(9999, "127.0.0.1", cb, auth_token="secret")
    handler = srv._handle_build

    async def run():
        req_no_auth = _fake_request({}, headers={})
        resp = await handler(req_no_auth)
        assert resp.status == 401
        req_bad = _fake_request({}, headers={"Authorization": "Bearer wrong"})
        assert (await handler(req_bad)).status == 401
        req_ok = _fake_request({"goal": "build x"}, headers={"Authorization": "Bearer secret"})
        resp = await handler(req_ok)
        assert resp.status == 200

    asyncio.run(run())


def test_http_rejects_empty_goal():
    async def cb(goal):
        return None

    srv = daemon.HTTPServer(9999, "127.0.0.1", cb, auth_token="")
    handler = srv._handle_build

    async def run():
        req = _fake_request({"goal": "  "}, headers={})
        resp = await handler(req)
        assert resp.status == 400

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Phase integration (no network: AsyncOpenAI patched with a fake client)
# ---------------------------------------------------------------------------


def _make_daemon_with_fake_client(reply_map=None):
    """LooperDaemon whose OpenAI client returns canned text per agent role.

    The fake inspects the system prompt (which contains the unique role
    string) to pick the right canned reply, because several agents share the
    same model in the real config and a model->agent map would be ambiguous.
    """
    replies = reply_map or {}
    role_to_key = {
        "Senior Technical Researcher": "researcher",
        "System Architect": "architect",
        "UX/API Designer": "ux_api_designer",
        "Code Builder": "builder",
        "Test Generator": "tester",
        "Senior Reviewer": "reviewer",
        "Security Auditor": "security_auditor",
        "Performance Optimizer": "performance_optimizer",
        "Documentation Writer": "documentation_writer",
        "Expert Fixer": "fixer",
    }

    def fake_factory(*args, **kwargs):
        async def _create(*a, **k):
            messages = k.get("messages", [])
            system_text = messages[0]["content"] if messages else ""
            agent_key = next(
                (key for role, key in role_to_key.items() if role in system_text),
                "builder",
            )
            content = replies.get(agent_key, "ok")
            msg = mock.AsyncMock()
            msg.message.content = content
            choice = mock.AsyncMock()
            choice.choices = [msg]
            return choice

        client = mock.AsyncMock()
        client.chat.completions.create.side_effect = _create
        return client

    with mock.patch.object(daemon, "openai") as fake_openai:
        fake_openai.AsyncOpenAI.side_effect = fake_factory
        d = daemon.LooperDaemon()
    return d


def test_full_pipeline_phases(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    daemon.configure({"workspace": "./w", "state_file": "./state.json"})
    d = _make_daemon_with_fake_client(
        {
            "researcher": "research notes",
            "architect": "architecture notes",
            "ux_api_designer": "api notes",
            "builder": "print('hello')",
            "tester": "def test_x(): assert True",
            "reviewer": "Score: 90",
            "security_auditor": "No issues found.",
            "documentation_writer": "# README",
            "fixer": "print('fixed')",
        }
    )

    async def run():
        # run each phase directly; all should write a file and return a dict
        for name in [
            "research",
            "architecture",
            "build",
            "review",
            "security_audit",
            "documentation",
        ]:
            handler = getattr(d.phases, f"run_{name}")
            res = await handler("goal")
            assert isinstance(res, dict)
            assert res.get("status") == "done"
        # fix phase, with a successful builder-equivalent reply
        fix = await d.phases.run_fix("goal", ["HIGH: bug"])
        assert fix["build_ok"] is True
        assert (d.phases.workspace / "src" / "generated_code.py").exists()

    asyncio.run(run())


def test_run_test_parses_summary(monkeypatch, tmp_path):
    """run_test must parse pytest output via parse_test_summary (no brittle
    substring counting). We stub the subprocess so the test does not spawn a
    nested pytest inside pytest (which disrupts output capture)."""
    monkeypatch.chdir(tmp_path)
    daemon.configure({"workspace": "./w", "state_file": "./state.json"})
    d = _make_daemon_with_fake_client({"tester": "def test_x(): assert True"})

    fake_proc = type(
        "P",
        (),
        {
            "stdout": "tests/test_gen.py::test_x PASSED [100%]\n2 passed in 0.01s",
            "stderr": "",
            "returncode": 0,
        },
    )()

    async def fake_to_thread(func, *args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    d.phases._write_file("src/generated_code.py", "x=1")

    async def run():
        res = await d.phases.run_test("goal")
        assert res["tests_passed"] == 2
        assert res["tests_total"] == 2

    asyncio.run(run())


def test_build_orchestrator_uses_fake_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    daemon.configure({"workspace": "./w", "state_file": "./state.json"})
    d = _make_daemon_with_fake_client(
        {
            "researcher": "r",
            "architect": "a",
            "ux_api_designer": "u",
            "builder": "x=1",
            "tester": "def test_x(): assert True",
            "reviewer": "Score: 95",
            "security_auditor": "No issues found.",
            "documentation_writer": "# doc",
            "fixer": "x=1",
        }
    )

    # Stub the test subprocess so we don't nest pytest-in-pytest.
    fake_proc = type(
        "P",
        (),
        {"stdout": "1 passed in 0.01s", "stderr": "", "returncode": 0},
    )()

    async def fake_to_thread(func, *args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    async def run():
        await d.build("make a calculator")
        assert d.state.state["status"] == "done"
        # With valid build + passing tests + 95 review + no security issues,
        # the score must be at or near the target.
        assert d.state.state["score"] >= 90.0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_daemon():
    """Build a LooperDaemon with the OpenAI client patched so it always fails
    (simulating a network/API outage) -- used to verify graceful handling.
    """
    with mock.patch.object(daemon, "openai") as fake_openai:
        fake_openai.AsyncOpenAI.return_value = _failing_client()
        d = daemon.LooperDaemon()
    return d


def _failing_client():
    client = mock.AsyncMock()

    async def _raise(*args, **kwargs):
        raise RuntimeError("simulated API outage")

    client.chat.completions.create.side_effect = _raise
    return client


def _fake_request(payload, headers):
    """Minimal aiohttp-like request stub."""
    import types

    req = types.SimpleNamespace()
    req.headers = headers

    async def json():
        return payload

    req.json = json
    req.app = {}
    return req
