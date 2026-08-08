"""CLI: argument handling, exit codes, logging, and graceful shutdown."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
import yaml

from looper.cli import (
    EXIT_BUILD_BELOW_MINIMUM,
    EXIT_CONFIG_ERROR,
    EXIT_COST_EXCEEDED,
    EXIT_OK,
    EXIT_OUT_OF_CREDITS,
    JSONLogFormatter,
    build_parser,
    main,
    setup_logging,
)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    payload = {
        "workspace": str(tmp_path / "ws"),
        "state_file": str(tmp_path / "state.json"),
        "watch_file": str(tmp_path / "cmds.txt"),
        "execution": {"max_cycles": 1, "min_acceptable": 50, "target_score": 60},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return path


@pytest.fixture
def no_network(monkeypatch):
    """Replace the real daemon's LLM + HTTP boundaries."""
    from looper import cli

    class StubDaemon:
        last_instance = None

        def __init__(self, config, **kwargs):
            self.config = config
            self.reset_called = False
            self.built = []
            self.score = 100.0
            StubDaemon.last_instance = self

            class State:
                def __init__(self, outer):
                    self.outer = outer

                def reset(self):
                    self.outer.reset_called = True

            self.state = State(self)

        async def build(self, goal):
            self.built.append(goal)
            return self.score

        async def start(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(cli, "LooperDaemon", StubDaemon)
    return StubDaemon


# --- Parser ------------------------------------------------------------------


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.config is None
    assert args.daemon is False
    assert args.log_level == "INFO"


def test_parser_accepts_all_flags():
    args = build_parser().parse_args(
        ["--goal", "g", "--config", "c.yaml", "--json-logs", "--log-level", "DEBUG"]
    )
    assert args.goal == "g"
    assert args.json_logs is True


def test_version_flag_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert "looper" in capsys.readouterr().out


# --- Exit codes --------------------------------------------------------------


def test_missing_config_returns_config_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main([]) == EXIT_CONFIG_ERROR


def test_invalid_config_returns_config_error(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"execution": {"max_cycles": 0}}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert main([]) == EXIT_CONFIG_ERROR


def test_check_config_succeeds(config_file, capsys):
    # setup_logging() intentionally takes over the root handlers, so assert on
    # the actual stderr stream rather than caplog.
    assert main(["--check-config"]) == EXIT_OK
    assert "Config OK" in capsys.readouterr().err


def test_check_config_reports_invalid(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"scoring": {"build": 1, "tests": 1, "security": 1, "review": 1}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["--check-config"]) == EXIT_CONFIG_ERROR


def test_reset_resets_state(config_file, no_network):
    assert main(["--reset"]) == EXIT_OK
    assert no_network.last_instance.reset_called is True


def test_goal_runs_one_build(config_file, no_network):
    assert main(["--goal", "build a CLI"]) == EXIT_OK
    assert no_network.last_instance.built == ["build a CLI"]


def test_low_score_returns_nonzero_exit(config_file, no_network, monkeypatch):
    """CI can gate on this: a below-minimum build is a failure."""
    from looper import cli

    class LowScore(no_network):
        async def build(self, goal):
            return 10.0

    monkeypatch.setattr(cli, "LooperDaemon", LowScore)
    assert main(["--goal", "g"]) == EXIT_BUILD_BELOW_MINIMUM


def test_no_action_prints_help(config_file, no_network, capsys):
    assert main([]) == EXIT_OK
    assert "usage:" in capsys.readouterr().out


def test_explicit_config_path(tmp_path, no_network, monkeypatch):
    path = tmp_path / "custom.yaml"
    path.write_text(yaml.safe_dump({"workspace": str(tmp_path / "w")}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["--config", str(path), "--check-config"]) == EXIT_OK


# --- Daemon mode -------------------------------------------------------------


def test_daemon_mode_shuts_down_cleanly(config_file, no_network, monkeypatch):
    from looper import cli

    async def immediate(daemon):
        return EXIT_OK

    monkeypatch.setattr(cli, "_run_daemon", immediate)
    assert main(["--daemon"]) == EXIT_OK


def test_run_daemon_cancels_on_stop_signal(config_file, no_network):
    from looper.cli import _run_daemon

    daemon = no_network(config=None)

    async def run():
        task = asyncio.ensure_future(_run_daemon(daemon))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "finished"

    assert asyncio.run(run()) in {"cancelled", "finished"}


def test_run_daemon_returns_when_serve_finishes(config_file, no_network):
    from looper.cli import _run_daemon

    class Quick(no_network):
        async def start(self):
            return None

    assert asyncio.run(_run_daemon(Quick(config=None))) == EXIT_OK


def test_run_daemon_reports_server_failure(config_file, no_network):
    from looper.cli import _run_daemon

    class Broken(no_network):
        async def start(self):
            raise RuntimeError("port in use")

    assert asyncio.run(_run_daemon(Broken(config=None))) == EXIT_CONFIG_ERROR


# --- Logging -----------------------------------------------------------------


def test_json_formatter_emits_valid_json():
    record = logging.LogRecord(
        name="looper.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    payload = json.loads(JSONLogFormatter().format(record))
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "looper.test"


def test_json_formatter_includes_exceptions():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    payload = json.loads(JSONLogFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


def test_setup_logging_replaces_handlers():
    setup_logging("DEBUG")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG
    setup_logging("INFO", json_logs=True)
    assert len(logging.getLogger().handlers) == 1
    assert isinstance(logging.getLogger().handlers[0].formatter, JSONLogFormatter)


def test_setup_logging_falls_back_on_bad_level():
    setup_logging("NOPE")
    assert logging.getLogger().level == logging.INFO


def test_run_daemon_registers_signal_handlers(config_file, no_network, monkeypatch):
    """Covers the SIGINT/SIGTERM registration branch and the stop path."""
    registered = []

    class FakeLoop:
        def create_future(self):
            return asyncio.get_event_loop().create_future()

        def add_signal_handler(self, sig, cb):
            registered.append(sig)
            cb()  # fire immediately so the daemon stops

    from looper import cli

    class Sleeper(no_network):
        async def start(self):
            await asyncio.sleep(3600)

    async def run():
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: FakeLoop())
        return await cli._run_daemon(Sleeper(config=None))

    assert asyncio.run(run()) == EXIT_OK
    assert registered  # at least SIGINT was registered


def test_run_daemon_tolerates_missing_signal_support(config_file, no_network, monkeypatch):
    """Windows selector loops raise NotImplementedError; must not crash."""

    class NoSignalLoop:
        def create_future(self):
            return asyncio.get_event_loop().create_future()

        def add_signal_handler(self, sig, cb):
            raise NotImplementedError

    from looper import cli

    class Quick(no_network):
        async def start(self):
            return None

    async def run():
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: NoSignalLoop())
        return await cli._run_daemon(Quick(config=None))

    assert asyncio.run(run()) == EXIT_OK


def test_run_daemon_ignores_absent_signals(config_file, no_network, monkeypatch):
    """getattr(signal, 'SIGTERM', None) is None on some platforms."""
    from looper import cli

    class NoSignals:
        SIGINT = None
        SIGTERM = None

    monkeypatch.setattr(cli, "signal", NoSignals)

    class Quick(no_network):
        async def start(self):
            return None

    assert asyncio.run(cli._run_daemon(Quick(config=None))) == EXIT_OK


def test_cost_budget_exceeded_returns_exit_4(config_file, no_network, monkeypatch):
    """#1: a build that blows its USD budget must exit 4, not keep spending."""
    from looper import cli
    from looper.config import CostBudgetExceeded

    class Broke(no_network):
        async def build(self, goal):
            raise CostBudgetExceeded(2.5, 1.0)

    monkeypatch.setattr(cli, "LooperDaemon", Broke)
    assert main(["--goal", "g"]) == EXIT_COST_EXCEEDED


def test_out_of_credits_returns_exit_6(config_file, no_network, monkeypatch):
    """#402: a build that hits OpenRouter 402 must exit 6 with a clear signal."""
    from looper import cli
    from looper.llm import OutOfCreditsError

    class Broke(no_network):
        async def build(self, goal):
            raise OutOfCreditsError("OpenRouter 402 Payment Required: account out of credits.")

    monkeypatch.setattr(cli, "LooperDaemon", Broke)
    assert main(["--goal", "g"]) == EXIT_OUT_OF_CREDITS
