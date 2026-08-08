"""The Orchestrator: deterministic control loop, no LLM of its own."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any

from looper.config import LooperConfig
from looper.llm import OpenRouterClient
from looper.phases import CycleEvidence, PhaseManager, PhaseResult
from looper.scoring import ScoreBreakdown, ScoringEngine
from looper.server import HTTPServer
from looper.state import StateManager
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
    ) -> None:
        self.config = config
        self.state = state or StateManager(config.state_file, config.execution.max_history_entries)
        self.client = client or OpenRouterClient(config.openrouter, config.retry)
        self.phases = phases or PhaseManager(config, self.state, self.client)
        self.scoring = ScoringEngine(config.scoring)
        self.watcher = watcher or FileWatcher(
            config.watch_file, self._on_command, config.watch_interval
        )
        self.http = server or HTTPServer(
            config.http, self._on_goal, status_provider=self.status_snapshot
        )
        self._build_lock = asyncio.Lock()
        self._running = False

    # -- Introspection ---------------------------------------------------

    def status_snapshot(self) -> dict[str, Any]:
        snapshot = self.state.snapshot()
        # History can be long; /status returns a bounded tail.
        snapshot["history"] = snapshot.get("history", [])[-20:]
        snapshot["build_in_progress"] = self._build_lock.locked()
        # Cost observability: an unattended daemon must report what it spends.
        snapshot["token_usage"] = self.client.total_usage.as_dict()
        snapshot["llm_calls"] = self.client.call_count
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
        execution = self.config.execution
        self.state.reset()
        self.state.update(current_goal=goal, status="running")
        self.state.save()

        final: ScoreBreakdown | None = None
        evidence = CycleEvidence()
        cycle = 0

        while cycle < execution.max_cycles:
            cycle += 1
            self.state.update(cycle=cycle)
            self.state.save()
            logger.info("=== CYCLE %d/%d ===", cycle, execution.max_cycles)

            phase_names = (
                self.config.first_cycle_phases if cycle == 1 else self.config.retry_cycle_phases
            )
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

        if score >= execution.min_acceptable:
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
            token_usage=self.client.total_usage.as_dict(),
        )
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
