"""Command-line entry point. All side effects live here, never at import."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Any, Sequence

from looper import __version__
from looper.config import ConfigError, CostBudgetExceeded, LooperConfig, load_config_with_dir
from looper.models import CatalogueUnavailableError, check_models, fetch_catalogue
from looper.orchestrator import LooperDaemon
from looper.sandbox import (
    SandboxUnavailableError,
    docker_available,
    podman_available,
    posix_rlimits_available,
    resolve_backend,
)
from looper.vcs import GitRepo

logger = logging.getLogger("looper.cli")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_BUILD_BELOW_MINIMUM = 3
EXIT_COST_EXCEEDED = 4
EXIT_SANDBOX_UNAVAILABLE = 5
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
        "--check-models",
        action="store_true",
        help="Verify every configured model slug against the live OpenRouter catalogue, and exit",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Report which sandbox/git capabilities this host actually provides, and exit",
    )
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


def run_check_models(config: LooperConfig) -> int:
    """Fail fast on a model slug OpenRouter does not serve.

    A bad slug is well-formed YAML, so ``--check-config`` cannot catch it; it
    surfaces mid-build instead, after earlier phases have already been billed.
    """
    try:
        catalogue = fetch_catalogue(config.openrouter.base_url)
    except CatalogueUnavailableError as exc:
        # An unreachable catalogue is not the same as a bad model. Say so, and
        # do not fail the check on a flaky network.
        logger.warning("Could not verify models: %s", exc)
        return EXIT_OK

    results = check_models({key: spec.model for key, spec in config.agents.items()}, catalogue)
    unknown = [r for r in results if not r.known]
    for result in results:
        logger.info(
            "%-22s %-34s %s", result.agent, result.model, "ok" if result.known else "NOT FOUND"
        )

    if unknown:
        logger.error(
            "%d model slug(s) are not served by OpenRouter: %s",
            len(unknown),
            ", ".join(f"{r.agent}={r.model}" for r in unknown),
        )
        return EXIT_CONFIG_ERROR
    logger.info("All %d model slugs verified against %s", len(results), config.openrouter.base_url)
    return EXIT_OK


def run_doctor(config: LooperConfig) -> int:
    """Print the isolation guarantees this host can actually deliver.

    The point is to make a weak host *visible before* a build runs: the old
    behaviour silently downgraded to no sandbox on Windows while the docs
    promised resource limits. Exits non-zero when the configured policy cannot
    be satisfied, so CI can gate on it.
    """
    execution = config.execution
    docker = docker_available()
    podman = podman_available()
    rlimits = posix_rlimits_available()
    git_ok = GitRepo(Path(config.workspace)).available()

    logger.info("looper %s doctor", __version__)
    logger.info("  platform            : %s", sys.platform)
    logger.info("  docker daemon       : %s", "yes" if docker else "no")
    logger.info("  podman machine      : %s", "yes" if podman else "no")
    logger.info("  POSIX rlimits       : %s", "yes" if rlimits else "no")
    logger.info("  git binary          : %s", "yes" if git_ok else "no")
    logger.info("  sandbox_backend     : %s", execution.sandbox_backend)
    logger.info("  sandbox_fail_closed : %s", execution.sandbox_fail_closed)
    logger.info("  artifact_mode       : %s", execution.artifact_mode)
    logger.info("  git_enabled         : %s", execution.git_enabled)

    if not execution.sandbox_tests:
        logger.warning("sandbox_tests is off: LLM-authored tests run unconfined")
        return EXIT_OK
    try:
        effective = resolve_backend(
            execution.sandbox_backend, fail_closed=execution.sandbox_fail_closed
        )
    except SandboxUnavailableError as exc:
        logger.error("SANDBOX UNAVAILABLE: %s", exc)
        logger.error("Builds will refuse to run generated tests. Install Docker or set")
        logger.error("execution.sandbox_fail_closed: false to accept the risk explicitly.")
        return EXIT_SANDBOX_UNAVAILABLE
    logger.info("  effective sandbox   : %s", effective)
    if effective == "none":
        logger.warning("No isolation in effect; generated tests run on the host")
    if execution.git_enabled and not git_ok:
        logger.warning("git_enabled is true but no git binary was found; commits will be skipped")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level, args.json_logs)

    try:
        config, config_dir = load_config_with_dir(args.config)
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

    if args.check_models:
        return run_check_models(config)

    if args.doctor:
        return run_doctor(config)

    daemon = LooperDaemon(config, config_dir=config_dir)

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
    except CostBudgetExceeded as exc:
        logger.error("Build aborted: %s", exc)
        return EXIT_COST_EXCEEDED
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        logger.info("Interrupted.")
        return EXIT_INTERRUPTED

    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
