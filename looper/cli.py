"""Command-line entry point. All side effects live here, never at import."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from looper import __version__
from looper.config import ConfigError, CostBudgetExceeded, LooperConfig, load_config_with_dir
from looper.dryrun import StubClient
from looper.llm import OutOfCreditsError
from looper.models import CatalogueUnavailableError, check_models, fetch_catalogue
from looper.orchestrator import LooperDaemon
from looper.report import REPORT_FILENAME, build_report, write_run_report, write_step_summary
from looper.sandbox import (
    SandboxUnavailableError,
    docker_available,
    podman_available,
    posix_rlimits_available,
    resolve_backend,
    wsl_available,
)
from looper.scaffold import DEFAULT_CONFIG_NAME, ScaffoldExistsError, write_starter_config
from looper.vcs import GitRepo

logger = logging.getLogger("looper.cli")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_BUILD_BELOW_MINIMUM = 3
EXIT_COST_EXCEEDED = 4
EXIT_SANDBOX_UNAVAILABLE = 5
EXIT_OUT_OF_CREDITS = 6
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
    parser.add_argument(
        "--init",
        nargs="?",
        const=DEFAULT_CONFIG_NAME,
        default=None,
        metavar="PATH",
        help=(
            "Write a minimal starter config (8 keys, everything else defaulted) "
            f"to PATH (default: {DEFAULT_CONFIG_NAME}) and exit. Never overwrites."
        ),
    )
    parser.add_argument("--goal", type=str, help="Run one build for this goal and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the whole pipeline with a local stub in place of the LLM: no API "
            "key, no network, no spend. Every gate (lint, adequacy, sandbox, "
            "scoring) still runs for real, so the verdict is honest."
        ),
    )
    parser.add_argument(
        "--report",
        nargs="?",
        const=REPORT_FILENAME,
        default=None,
        metavar="PATH",
        help=(
            "Write a machine-readable run report (score breakdown, spend per "
            f"model, per-phase history) to PATH (default: {REPORT_FILENAME}). "
            "When $GITHUB_STEP_SUMMARY is set, a Markdown summary is appended "
            "to it as well."
        ),
    )
    parser.add_argument("--daemon", action="store_true", help="Run continuously (24/7)")
    parser.add_argument("--reset", action="store_true", help="Reset persisted state and exit")
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Skip research/architecture when the saved state shows they already "
            "completed for this exact --goal and their artifacts survive"
        ),
    )
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
    wsl = wsl_available()
    git_ok = GitRepo(Path(config.workspace)).available()

    logger.info("looper %s doctor", __version__)
    logger.info("  platform            : %s", sys.platform)
    logger.info("  docker daemon       : %s", "yes" if docker else "no")
    logger.info("  podman machine      : %s", "yes" if podman else "no")
    logger.info("  POSIX rlimits       : %s", "yes" if rlimits else "no")
    logger.info("  WSL distro          : %s", "yes" if wsl else "no")
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
        logger.error("Builds will refuse to run generated tests. Fix with ONE of:")
        logger.error("  * install Docker Desktop (strongest: no network, read-only rootfs)")
        logger.error("  * run `wsl --install` for a WSL2 distro (resource limits, shared network)")
        logger.error("  * set execution.sandbox_fail_closed: false to accept the risk explicitly")
        return EXIT_SANDBOX_UNAVAILABLE
    logger.info("  effective sandbox   : %s", effective)
    if effective == "none":
        logger.warning("No isolation in effect; generated tests run on the host")
    if effective == "wsl":
        logger.warning(
            "WSL sandbox bounds CPU/memory but shares the host network and can "
            "reach the Windows filesystem via /mnt; Docker is stronger"
        )
    if execution.git_enabled and not git_ok:
        logger.warning("git_enabled is true but no git binary was found; commits will be skipped")
    return EXIT_OK


def run_init(path: str) -> int:
    """Write a starter config, refusing to clobber an existing one."""
    try:
        written = write_starter_config(Path(path))
    except ScaffoldExistsError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR
    except OSError as exc:
        logger.error("Could not write %s: %s", path, exc)
        return EXIT_CONFIG_ERROR
    logger.info("Wrote starter config to %s", written)
    logger.info("Next steps:")
    logger.info('  1. looper --config %s --dry-run --goal "build a CLI todo app"', written)
    logger.info("     (no API key needed: the whole gate runs against a local stub)")
    logger.info("  2. export OPENROUTER_API_KEY=... then looper --check-models")
    logger.info("  3. looper --doctor    # what isolation can this host give?")
    return EXIT_OK


def emit_report(
    *,
    destination: str,
    daemon: LooperDaemon,
    config: LooperConfig,
    goal: str,
    score: float,
    exit_code: int,
    dry_run: bool,
    env: Mapping[str, str] | None = None,
) -> None:
    """Write the run report, and the CI step summary when running in CI."""
    state = daemon.state.state
    report = build_report(
        goal=goal,
        status=str(state.get("status", "")),
        score=score,
        min_acceptable=config.execution.min_acceptable,
        target_score=config.execution.target_score,
        cycles=int(state.get("cycle", 0) or 0),
        exit_code=exit_code,
        score_breakdown=state.get("score_breakdown") or {},
        cost_usd=daemon.client.running_cost_usd(),
        cost_by_model=daemon.client.cost_by_model(),
        token_usage=daemon.client.total_usage.as_dict(),
        llm_calls=daemon.client.call_count,
        phases=state.get("history") or [],
        artifacts=state.get("files_created") or [],
        dry_run=dry_run,
    )
    write_run_report(report, Path(destination))
    source = os.environ if env is None else env
    summary_path = source.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        write_step_summary(report, Path(summary_path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level, args.json_logs)

    # Scaffolding runs BEFORE the config is loaded: the entire point of
    # --init is that there is no config to load yet.
    if args.init is not None:
        return run_init(args.init)

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

    client = StubClient(config) if args.dry_run else None
    if args.dry_run:
        logger.info(
            "DRY RUN: every agent is answered by a local stub. No API key is used, "
            "no request leaves this machine, and spend stays at $0.00. Every gate "
            "still runs for real, so the verdict below is honest."
        )
    daemon = LooperDaemon(config, config_dir=config_dir, resume=args.resume, client=client)

    if args.reset:
        daemon.state.reset()
        logger.info("State reset.")
        return EXIT_OK

    exit_code = EXIT_OK
    score = 0.0
    try:
        if args.goal:
            score = asyncio.run(daemon.build(args.goal))
            logger.info("Final score: %.2f", score)
            exit_code = (
                EXIT_OK if score >= config.execution.min_acceptable else EXIT_BUILD_BELOW_MINIMUM
            )
        elif args.daemon:
            return asyncio.run(_run_daemon(daemon))
        else:
            parser.print_help()
            return EXIT_OK
    except CostBudgetExceeded as exc:
        logger.error("Build aborted: %s", exc)
        exit_code = EXIT_COST_EXCEEDED
    except OutOfCreditsError as exc:
        logger.error("Build aborted: %s", exc)
        exit_code = EXIT_OUT_OF_CREDITS
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        logger.info("Interrupted.")
        return EXIT_INTERRUPTED

    # The report describes the build regardless of how it ended: a run that
    # hit the cost ceiling is exactly the run whose spend breakdown someone
    # wants to read.
    if args.report is not None:
        _write_report(args, daemon, config, score, exit_code)
    return exit_code


def _write_report(
    args: argparse.Namespace,
    daemon: LooperDaemon,
    config: LooperConfig,
    score: float,
    exit_code: int,
) -> None:
    """Thin wrapper so the report call is one traceable statement."""
    emit_report(
        destination=args.report,
        daemon=daemon,
        config=config,
        goal=args.goal or "",
        score=score,
        exit_code=exit_code,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
