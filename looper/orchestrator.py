"""The Orchestrator: deterministic control loop, no LLM of its own."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from looper.config import CostBudgetExceeded, LooperConfig
from looper.llm import OpenRouterClient, OutOfCreditsError
from looper.notify import Notifier
from looper.phases import CycleEvidence, PhaseManager, PhaseResult
from looper.scoring import ScoreBreakdown, ScoringEngine
from looper.server import HTTPServer
from looper.state import StateManager, build_checkpoint
from looper.vcs import BuildRepo, GitRepo
from looper.watcher import FileWatcher

logger = logging.getLogger("looper.orchestrator")


#: Phases a resume may skip. Deliberately excludes every phase that produces
#: *scored evidence* (build, test, review, security_audit): ADR-004 says a
#: cycle may only score facts it re-verified, so skipping review would bank a
#: previous run's 92 without re-earning it -- exactly the hole
#: ``CycleEvidence.invalidate_unverified`` exists to close. What is skippable
#: is the expensive *input* work: research and architecture are read by later
#: phases but contribute no points themselves, and together they are the two
#: most costly agents in cycle 1.
RESUMABLE_PHASES: frozenset[str] = frozenset({"research", "architecture"})

#: Phases that may run concurrently with each other. ``review`` and
#: ``security_audit`` both read the finished artifact and write to separate
#: files; neither reads the other's output, and each contributes an
#: independent term to the score. Running them serially cost a full agent
#: round-trip of wall clock for no ordering benefit.
#:
#: Deliberately a *small allowlist* rather than a dependency solver. Every
#: other phase is genuinely sequential -- build reads architecture, test reads
#: build, fix reads the review findings -- and a wrong guess here would score
#: a review of code the builder had not finished writing. Adding a phase to
#: this set is a claim that it reads nothing its co-runner writes (ADR-018).
PARALLEL_PHASE_GROUP: frozenset[str] = frozenset({"review", "security_audit"})


def _parallel_batches(phase_names: Sequence[str]) -> list[tuple[str, ...]]:
    """Group ``phase_names`` into execution batches, preserving order.

    Consecutive members of :data:`PARALLEL_PHASE_GROUP` collapse into one
    batch; everything else is a batch of one. Only *adjacent* phases are
    grouped, so reordering the configured phase list can never silently move
    a phase past a dependency it needs.
    """
    batches: list[tuple[str, ...]] = []
    current: list[str] = []
    for name in phase_names:
        if name in PARALLEL_PHASE_GROUP:
            current.append(name)
            continue
        if current:
            batches.append(tuple(current))
            current = []
        batches.append((name,))
    if current:
        batches.append(tuple(current))
    return batches


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
        notifier: Notifier | None = None,
        resume: bool = False,
    ) -> None:
        self.config = config
        self._config_dir = Path(config_dir) if config_dir else None
        self._resume = resume
        self.state = state or StateManager(config.state_file, config.execution.max_history_entries)
        self.client = client or OpenRouterClient(
            config.openrouter,
            config.retry,
            model_prices_usd_per_1k=config.execution.model_prices_usd_per_1k,
            completion_prices_usd_per_1k=config.execution.completion_prices_usd_per_1k,
            default_token_price_usd=config.execution.default_token_price_usd,
            max_cost_usd=config.execution.max_cost_usd,
        )
        self.phases = phases or PhaseManager(
            config, self.state, self.client, config_dir=self._config_dir
        )
        self.scoring = ScoringEngine(config.scoring)
        self.notifier = notifier or Notifier(config.notifications)
        self.watcher = watcher or FileWatcher(
            config.watch_file, self._on_command, config.watch_interval
        )
        self.http = server or HTTPServer(
            config.http, self._on_goal, status_provider=self.status_snapshot
        )
        self._build_lock = asyncio.Lock()
        self._running = False
        #: Phases the current build inherited from a resume checkpoint and
        #: must not pay for again. Reset at the start of every build.
        self._skip_phases: set[str] = set()
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
        # History can be long; /status returns a bounded tail. Trimming inside
        # snapshot() avoids deep-copying 500 entries to then show 20.
        snapshot = self.state.snapshot(history_limit=20)
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

    def resumable_phases(self, goal: str) -> tuple[str, ...]:
        """Phases already paid for and provable on disk for ``goal``.

        Empty when resume is off, when the checkpoint belongs to a *different*
        goal, or when the previous run actually finished. Resuming across
        goals would hand cycle 1 an architecture written for something else,
        which is worse than paying for it again -- so the goal must match
        exactly.
        """
        if not self._resume:
            return ()
        checkpoint = build_checkpoint(self.state.state)
        if checkpoint["goal"] != goal:
            if checkpoint["goal"]:
                logger.info(
                    "Not resuming: checkpoint is for a different goal (%r)", checkpoint["goal"]
                )
            return ()
        if checkpoint["status"] == "done":
            logger.info("Not resuming: the previous run for this goal completed")
            return ()
        done = tuple(checkpoint["completed_phases"])
        # Scored phases are never resumable (ADR-004): only unscored input
        # work may be skipped, or a resumed cycle would bank evidence it
        # never re-verified.
        eligible = tuple(p for p in done if p in RESUMABLE_PHASES)
        # A phase is only resumable if its artifact still exists. A wiped
        # workspace with a stale state file would otherwise skip straight to
        # `build` with no design document to read.
        surviving = tuple(p for p in eligible if self.phases.phase_artifact_exists(p))
        missing = [p for p in eligible if p not in surviving]
        if missing:
            logger.warning(
                "Checkpoint lists %s but their artifacts are gone; re-running them", missing
            )
        return surviving

    async def _build_locked(self, goal: str) -> float:
        try:
            score = await self._build_phases(goal)
        except CostBudgetExceeded as exc:
            # The ceiling is now enforced inside the LLM client, so it can fire
            # mid-cycle rather than only at the loop guard. Persist the reason
            # before it propagates, or /status would still read "running" for a
            # build that has already stopped.
            self.state.update(status="cost_exhausted")
            self.state.save()
            self._notify_outcome(goal, "cost_exhausted", detail=str(exc))
            raise
        except OutOfCreditsError as exc:
            # Status is already persisted by _run_phases; this only adds the
            # outbound notification, which must not be skipped just because
            # the abort came from a phase rather than the budget guard.
            self._notify_outcome(goal, "out_of_credits", detail=str(exc))
            raise
        self._notify_outcome(
            goal,
            "passed" if score >= self.config.execution.min_acceptable else "below_minimum",
            score=score,
        )
        return score

    def _notify_outcome(
        self, goal: str, status: str, *, score: float | None = None, detail: str = ""
    ) -> None:
        """Best-effort webhook post. Never raises, never affects the score."""
        self.notifier.notify(
            status=status,
            goal=goal,
            score=float(self.state.state.get("score", 0.0)) if score is None else score,
            cycle=int(self.state.state.get("cycle", 0) or 0),
            cost_usd=self.client.running_cost_usd(),
            detail=detail,
        )

    async def _build_phases(self, goal: str) -> float:
        execution = self.config.execution
        # Compute the checkpoint BEFORE reset() wipes it. This ordering is the
        # whole feature: reset() is what makes each build independent, so the
        # resume decision has to be taken against the state as it was found.
        resumable = self.resumable_phases(goal)
        self.state.reset()
        self.state.update(current_goal=goal, status="running")
        if resumable:
            logger.info(
                "Resuming: skipping %d already-completed phase(s): %s",
                len(resumable),
                ", ".join(resumable),
            )
            for phase in resumable:
                self.state.record_completed_phase(phase)
        self._skip_phases = set(resumable)
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
        """Run ``phase_names`` in order, concurrently where it is safe.

        Results are absorbed in the batch's declared order, never in
        completion order: scoring must not depend on which agent answered
        first, or a build's score would vary run to run for the same replies.
        """
        for batch in _parallel_batches(phase_names):
            runnable = [name for name in batch if self._admit_phase(name)]
            if not runnable:
                continue
            if len(runnable) == 1:
                logger.info("Phase: %s", runnable[0])
                results = [await self._invoke_phase(runnable[0], goal)]
            else:
                logger.info("Phases (parallel): %s", ", ".join(runnable))
                results = list(
                    await asyncio.gather(*(self._invoke_phase(name, goal) for name in runnable))
                )
            for name, result in zip(runnable, results):
                if result is None:
                    continue
                self._log(result)
                evidence.absorb(result)
                if result.ok:
                    # Checkpoint only successful phases: a failed phase left a
                    # partial or error-text artifact on disk, and resuming onto
                    # that would silently feed garbage to the next phase.
                    self.state.record_completed_phase(name)
                    self.state.save()
                if result.out_of_credits:
                    self._abort_out_of_credits(result)
        return evidence

    def _admit_phase(self, phase_name: str) -> bool:
        """False when ``phase_name`` is skipped by a resume checkpoint."""
        if phase_name not in self._skip_phases:
            return True
        logger.info("Phase: %s (skipped, resumed from checkpoint)", phase_name)
        # Consume the skip: it is valid once, for the cycle that inherited
        # it. Leaving it set would skip research in every later cycle too,
        # which the retry phase list may legitimately want to re-run.
        self._skip_phases.discard(phase_name)
        return False

    async def _invoke_phase(self, phase_name: str, goal: str) -> PhaseResult | None:
        """Dispatch one phase. ``None`` when no handler exists for it."""
        handler = getattr(self.phases, f"run_{phase_name}", None)
        if handler is None:
            logger.warning("Unknown phase %r; skipping", phase_name)
            return None
        result: PhaseResult = await handler(goal)
        return result

    def _abort_out_of_credits(self, result: PhaseResult) -> None:
        """Stop the build on a 402: no later call can succeed either."""
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
