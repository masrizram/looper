"""The Orchestrator: deterministic control loop, no LLM of its own."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from looper.config import CostBudgetExceeded, LooperConfig
from looper.llm import OpenRouterClient, OutOfCreditsError
from looper.phases import CycleEvidence, PhaseManager, PhaseResult
from looper.scoring import ScoreBreakdown, ScoringEngine
from looper.server import HTTPServer
from looper.state import StateManager
from looper.vcs import BuildRepo, GitRepo
from looper.watcher import FileWatcher

logger = logging.getLogger("looper.orchestrator")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class LooperDaemon:
    """Wires the components together and runs the build loop.

    Everything is injectable so tests never need to patch module globals.
    """

    def __init__(
        self,
        config: LooperConfig,
        *,
        state: StateManager | None = None,
        client: OpenRouterClient | None = None,
        phases: PhaseManager | None = None,
        server: HTTPServer | None = None,
        watcher: FileWatcher | None = None,
        vcs: BuildRepo | None = None,
        config_dir: Path | None = None,
    ) -> None:
        self.config = config
        self._config_dir = Path(config_dir) if config_dir else None
        self.state = state or StateManager(config.state_file, config.execution.max_history_entries)
        self.client = client or OpenRouterClient(
            config.openrouter,
            config.retry,
            model_prices_usd_per_1k=config.execution.model_prices_usd_per_1k,
            default_token_price_usd=config.execution.default_token_price_usd,
            max_cost_usd=config.execution.max_cost_usd,
        )
        self.phases = phases or PhaseManager(
            config, self.state, self.client, config_dir=self._config_dir
        )
        self.scoring = ScoringEngine(config.scoring)
        self.watcher = watcher or FileWatcher(
            config.watch_file, self._on_command, config.watch_interval
        )
        self.http = server or HTTPServer(
            config.http, self._on_goal, status_provider=self.status_snapshot
        )
        self._build_lock = asyncio.Lock()
        self._running = False
        self.vcs = vcs or self._default_vcs()

    def _default_vcs(self) -> BuildRepo | None:
        """Build a git session when enabled, else None (feature stays off)."""
        execution = self.config.execution
        if not execution.git_enabled:
            return None
        return BuildRepo(
            GitRepo(
                Path(self.config.workspace),
                author_name=execution.git_author_name,
                author_email=execution.git_author_email,
            ),
            branch_prefix=execution.git_branch_prefix,
        )

    # -- Introspection ---------------------------------------------------

    def status_snapshot(self) -> dict[str, Any]:
        snapshot = self.state.snapshot()
        # History can be long; /status returns a bounded tail.
        snapshot["history"] = snapshot.get("history", [])[-20:]
        snapshot["build_in_progress"] = self._build_lock.locked()
        # Cost observability: an unattended daemon must report what it spends.
        snapshot["token_usage"] = self.client.total_usage.as_dict()
        snapshot["llm_calls"] = self.client.call_count
        snapshot["cost_usd"] = self.client.running_cost_usd()
        snapshot["cost_by_model"] = self.client.cost_by_model()
        return snapshot

    # -- Lifecycle -------------------------------------------------------

    async def start(self) -> None:
        logger.info(
            "Starting daemon: HTTP %s:%s, watching %s",
            self.config.http.bind,
            self.config.http.port,
            self.config.watch_file,
        )
        self._running = True
        await self.http.start()
        watcher_task = asyncio.ensure_future(self.watcher.start())
        try:
            await watcher_task
        except asyncio.CancelledError:
            logger.info("Daemon cancelled; shutting down")
            raise
        finally:
            self._running = False
            self.watcher.stop()
            watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher_task
            await self.http.stop()

    async def _on_command(self, content: str) -> None:
        logger.info("Command received: %.100s", content)
        await self.build(content)

    async def _on_goal(self, goal: str) -> None:
        logger.info("HTTP goal received: %.100s", goal)
        await self.build(goal)

    # -- Build loop ------------------------------------------------------

    async def build(self, goal: str) -> float:
        """Run the full pipeline for ``goal``. Returns the final score.

        Serialised by a lock: two concurrent triggers previously interleaved
        into one shared StateManager and corrupted cycle/score/history.
        """
        async with self._build_lock:
            return await self._build_locked(goal)

    async def _build_locked(self, goal: str) -> float:
        try:
            return await self._build_phases(goal)
        except CostBudgetExceeded:
            # The ceiling is now enforced inside the LLM client, so it can fire
            # mid-cycle rather than only at the loop guard. Persist the reason
            # before it propagates, or /status would still read "running" for a
            # build that has already stopped.
            self.state.update(status="cost_exhausted")
            self.state.save()
            raise

    async def _build_phases(self, goal: str) -> float:
        execution = self.config.execution
        self.state.reset()
        self.state.update(current_goal=goal, status="running")
        self.state.save()

        final: ScoreBreakdown | None = None
        evidence = CycleEvidence()
        cycle = 0

        if self.vcs is not None:
            branch = await asyncio.to_thread(self.vcs.start, goal)
            if branch:
                logger.info("Recording this build on git branch %s", branch)
                self.state.update(git=self.vcs.as_dict())
                self.state.save()

        while cycle < execution.max_cycles:
            # Hard cost ceiling: abort before spending more, rather than
            # silently running up the API bill. ADR-005.
            if (
                execution.max_cost_usd > 0
                and self.client.running_cost_usd() >= execution.max_cost_usd
            ):
                logger.error(
                    "Cost budget %.2f USD exhausted at cycle %d (spent %.2f); aborting.",
                    execution.max_cost_usd,
                    cycle,
                    self.client.running_cost_usd(),
                )
                self.state.update(status="cost_exhausted")
                self.state.save()
                raise CostBudgetExceeded(self.client.running_cost_usd(), execution.max_cost_usd)

            cycle += 1
            self.state.update(cycle=cycle)
            self.state.save()
            logger.info("=== CYCLE %d/%d ===", cycle, execution.max_cycles)

            phase_names = (
                self.config.first_cycle_phases if cycle == 1 else self.config.retry_cycle_phases
            )
            if cycle > 1:
                # Only evidence this cycle re-verifies may count toward its score.
                evidence.invalidate_unverified(phase_names)
            evidence = await self._run_phases(goal, phase_names, evidence)

            final = self.scoring.calculate(
                build_ok=evidence.build_ok,
                tests_passed=evidence.tests_passed,
                tests_total=evidence.tests_total,
                security_issues=evidence.security_issues,
                review_score=evidence.review_score,
            )
            self.state.update(score=final.total, score_breakdown=final.as_dict())
            self.state.save()
            logger.info("Score: %.2f %s", final.total, final.as_dict())

            if self.vcs is not None and execution.git_commit_per_cycle:
                sha = await asyncio.to_thread(
                    self.vcs.record_cycle, cycle, final.total, final.summary_line()
                )
                if sha:
                    logger.info("Cycle %d committed as %s", cycle, sha)
                    self.state.update(git=self.vcs.as_dict())
                    self.state.save()

            if final.total >= execution.target_score:
                logger.info("Target score reached; stopping early")
                break

            # The `while` guard already ends the run after the last cycle, so
            # only attempt a fix when another cycle will actually follow.
            if cycle < execution.max_cycles and final.total < execution.min_acceptable:
                issues = [
                    f"Review score was {evidence.review_score}/100 "
                    f"(target {execution.target_score})",
                    *evidence.security_issues,
                ]
                fix_result = await self.phases.run_fix(goal, issues)
                self._log(fix_result)
                evidence.absorb(fix_result)

        score = final.total if final else 0.0

        artifacts_complete = score >= execution.min_acceptable
        if artifacts_complete:
            await self._run_phases(goal, self.config.final_phases, evidence)
        else:
            logger.warning(
                "Final score %.2f below minimum %.2f; skipping performance/documentation polish.",
                score,
                execution.min_acceptable,
            )

        self.state.update(
            status="done",
            current_phase="done",
            score=score,
            # A log line is invisible to anyone reading /status or the state
            # file, so an operator could not tell a complete artifact from one
            # missing its docs and optimisation passes.
            artifacts_complete=artifacts_complete,
            token_usage=self.client.total_usage.as_dict(),
        )
        if self.vcs is not None:
            sha = await asyncio.to_thread(self.vcs.record_cycle, cycle, score, "final artifact")
            if sha:
                logger.info("Final artifact committed as %s", sha)
            self.state.update(git=self.vcs.as_dict())
        self.state.save()
        logger.info("Build complete. Final score: %.2f", score)
        return score

    async def _run_phases(
        self,
        goal: str,
        phase_names: tuple[str, ...],
        evidence: CycleEvidence,
    ) -> CycleEvidence:
        for phase_name in phase_names:
            handler = getattr(self.phases, f"run_{phase_name}", None)
            if handler is None:
                logger.warning("Unknown phase %r; skipping", phase_name)
                continue
            logger.info("Phase: %s", phase_name)
            result = await handler(goal)
            self._log(result)
            evidence.absorb(result)

            # Hard stop: a 402 (account out of credits) cannot be retried into
            # success. Continuing would only burn the remaining phases and
            # cycles on a condition that will not change, so abort with a clear
            # message instead of grinding to a score of 0.
            if result.out_of_credits:
                logger.error(
                    "=== BUILD ABORTED: OUT OF CREDITS ===\n"
                    "OpenRouter returned 402 Payment Required for the '%s' phase.\n"
                    "The account has no credits, so every further agent call would\n"
                    "fail identically. Add credits at https://openrouter.ai/settings/credits\n"
                    "then re-run the build.",
                    result.phase,
                )
                self.state.update(status="out_of_credits")
                self.state.save()
                raise OutOfCreditsError(
                    "OpenRouter 402 Payment Required: account out of credits. "
                    "Add credits at https://openrouter.ai/settings/credits"
                )
        return evidence

    def _log(self, result: PhaseResult) -> None:
        entry = {
            "timestamp": _utcnow(),
            "cycle": int(self.state.state.get("cycle", 0)),
            **result.as_dict(),
        }
        self.state.append_history(entry)
        self.state.save()
        logger.info(
            "phase=%s status=%s summary=%s",
            entry["phase"],
            entry["status"],
            entry["summary"],
        )
