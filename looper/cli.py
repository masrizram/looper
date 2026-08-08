"""Command-line entry point. All side effects live here, never at import."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from typing import Any, Sequence

from looper import __version__
from looper.config import ConfigError, LooperConfig, load_config
from looper.orchestrator import LooperDaemon

logger = logging.getLogger("looper.cli")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_BUILD_BELOW_MINIMUM = 3
EXIT_INTERRUPTED = 130


class JSONLogFormatter(logging.Formatter):
    """One JSON object per line - greppable and machine-parseable."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if json_logs:
        handler.setFormatter(JSONLogFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="looper",
        description="Looper - autonomous multi-agent software engineering daemon",
    )
    parser.add_argument("--version", action="version", version=f"looper {__version__}")
    parser.add_argument("--config", type=str, default=None, help="Path to config YAML")
    parser.add_argument("--goal", type=str, help="Run one build for this goal and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously (24/7)")
    parser.add_argument("--reset", action="store_true", help="Reset persisted state and exit")
    parser.add_argument("--check-config", action="store_true", help="Validate config and exit")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    parser.add_argument("--json-logs", action="store_true", help="Emit logs as JSON lines")
    return parser


async def _run_daemon(daemon: LooperDaemon) -> int:
    """Run until SIGINT/SIGTERM, then shut down cleanly."""
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def request_stop() -> None:
        if not stop.done():
            stop.set_result(None)

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            # Windows selector loops lack add_signal_handler; KeyboardInterrupt
            # still unwinds through asyncio.run.
            pass

    serve = asyncio.ensure_future(daemon.start())
    done, _ = await asyncio.wait({serve, stop}, return_when=asyncio.FIRST_COMPLETED)

    if serve in done:
        return EXIT_OK if not serve.exception() else EXIT_CONFIG_ERROR

    serve.cancel()
    try:
        await serve
    except asyncio.CancelledError:
        pass
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level, args.json_logs)

    try:
        config: LooperConfig = load_config(args.config)
    except (ConfigError, FileNotFoundError) as exc:
        logger.error("Configuration error: %s", exc)
        return EXIT_CONFIG_ERROR

    if args.check_config:
        logger.info(
            "Config OK: workspace=%s bind=%s:%s agents=%d",
            config.workspace,
            config.http.bind,
            config.http.port,
            len(config.agents),
        )
        return EXIT_OK

    daemon = LooperDaemon(config)

    if args.reset:
        daemon.state.reset()
        logger.info("State reset.")
        return EXIT_OK

    try:
        if args.goal:
            score = asyncio.run(daemon.build(args.goal))
            logger.info("Final score: %.2f", score)
            return EXIT_OK if score >= config.execution.min_acceptable else EXIT_BUILD_BELOW_MINIMUM
        if args.daemon:
            return asyncio.run(_run_daemon(daemon))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        logger.info("Interrupted.")
        return EXIT_INTERRUPTED

    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
