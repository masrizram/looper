"""Agent-driven pipeline stages.

One method per phase, all sharing the ``_run_agent_phase`` template. This
module decides *what to ask an agent and how to judge the answer*; it defers
filesystem writes to :mod:`looper.phases.workspace` and anything that runs
untrusted code to :mod:`looper.phases.execution`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from looper.artifact import parse_multifile, primary_module, verify_python_files
from looper.config import LooperConfig
from looper.llm import AgentReply, OpenRouterClient
from looper.phases.results import PhaseResult, WorkspaceEscapeError, replace_result
from looper.phases.workspace import (
    CODE_FILE,
    DESIGN_FILE,
    DOCS_FILE,
    OPTIMIZED_FILE,
    RESEARCH_FILE,
    REVIEW_FILE,
    SECURITY_FILE,
    TESTS_FILE,
    strip_code_fences,
)
from looper.prompts import PromptGenerator
from looper.scoring import parse_security_findings, reports_no_issues
from looper.state import StateManager

logger = logging.getLogger("looper.phases")

REVIEW_SCORE_RE = re.compile(r"score\s*[:=]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


class AgentPhasesMixin:
    """The nine pipeline stages, expressed on top of one template method."""

    config: LooperConfig
    state: StateManager
    client: OpenRouterClient
    prompts: PromptGenerator
    workspace: Path

    # Provided by the sibling mixins; declared for type checking.
    def write_file(self, relative_path: str, content: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def read_file(self, relative_path: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def _verify_syntax(self, reply: AgentReply) -> tuple[bool, str]:  # pragma: no cover
        raise NotImplementedError

    def _lint_generated(self, relative_path: str) -> tuple[bool, str]:  # pragma: no cover
        raise NotImplementedError

    async def _execute_generated_tests(self) -> tuple[int, int, str]:  # pragma: no cover
        raise NotImplementedError

    async def _run_user_tests(self) -> tuple[int, int, str]:  # pragma: no cover
        raise NotImplementedError

    # -- Template method -------------------------------------------------

    async def _run_agent_phase(
        self,
        *,
        phase: str,
        agent_key: str,
        prompt: str,
        output_file: str,
        extra_system: str = "",
    ) -> tuple[AgentReply, PhaseResult]:
        """Shared skeleton: mark in-progress, call agent, persist, save.

        Nine near-identical ``run_*`` bodies collapsed into this. Subclasses of
        behaviour that differ (parsing a score, running pytest) layer on top of
        the returned :class:`AgentReply`.
        """
        agent = self.config.agents[agent_key]
        self.state.update(current_phase=phase, status="in_progress")

        reply = await self.client.call(agent, prompt, extra_system=extra_system)
        written = self.write_file(output_file, reply.text)

        if reply.failed:
            self.state.record_error(f"{phase}: {reply.error}")
            if reply.out_of_credits:
                logger.error(
                    "OUT OF CREDITS: OpenRouter returned 402 for the %s agent. "
                    "The build cannot continue until credits are added at "
                    "https://openrouter.ai/settings/credits - aborting this phase.",
                    agent.role,
                )

        result = PhaseResult(
            phase=phase,
            agent=agent.role,
            model=agent.model,
            ok=reply.ok,
            summary=("" if reply.ok else f"{agent.role} failed: {reply.error}"),
            files_created=(written,),
            error=reply.error,
            out_of_credits=reply.out_of_credits,
        )
        self.state.update(status=result.status)
        self.state.save()
        return reply, result

    # -- Phases ----------------------------------------------------------

    async def run_research(self, goal: str) -> PhaseResult:
        reply, result = await self._run_agent_phase(
            phase="research",
            agent_key="researcher",
            prompt=self.prompts.research(goal),
            output_file=RESEARCH_FILE,
        )
        summary = "Research completed" if reply.ok else result.summary
        return replace_result(result, summary=summary)

    async def run_architecture(self, goal: str) -> PhaseResult:
        research = self.read_file(RESEARCH_FILE)
        architect = self.config.agents["architect"]
        designer = self.config.agents["ux_api_designer"]

        self.state.update(current_phase="architecture", status="in_progress")
        design = await self.client.call(architect, self.prompts.architecture(goal, research))
        api_notes = await self.client.call(designer, self.prompts.api_design(goal, design.text))

        combined = f"{design.text}\n\n## API & DX Design (UX/API Designer)\n\n{api_notes.text}"
        written = self.write_file(DESIGN_FILE, combined)

        ok = design.ok and api_notes.ok
        error = design.error or api_notes.error
        if not ok:
            self.state.record_error(f"architecture: {error}")

        self.state.update(status="done" if ok else "error")
        self.state.save()
        return PhaseResult(
            phase="architecture",
            agent=f"{architect.role} + {designer.role}",
            model=f"{architect.model} + {designer.model}",
            ok=ok,
            summary=("Architecture + API/UX design completed" if ok else f"Design failed: {error}"),
            files_created=(written,),
            error=error,
        )

    async def run_build(self, goal: str) -> PhaseResult:
        architecture = self.read_file(DESIGN_FILE)
        package_mode = self.config.execution.artifact_mode == "package"
        reply, result = await self._run_agent_phase(
            phase="build",
            agent_key="builder",
            prompt=self.prompts.build(goal, architecture, package_mode=package_mode),
            output_file=CODE_FILE,
        )
        if package_mode:
            packaged = self._persist_package(reply)
            if packaged is not None:
                return replace_result(result, **packaged)
        build_ok, note = self._verify_syntax(reply)
        summary = "Code generated" if build_ok else (note or result.summary)
        if reply.ok and not build_ok:
            self.state.record_error(f"build: {note}")
            self.state.save()
            return replace_result(result, build_ok=False, summary=summary)
        # Persist the fence-stripped source (agents wrap code in ```python
        # fences) so the on-disk file is valid Python for the lint gate and the
        # test phase's import, rather than the raw fenced blob.
        if build_ok:
            self.write_file(CODE_FILE, strip_code_fences(reply.text))
        # Style/compile gate so obviously-broken or malformed output never
        # reaches the "done" state even when it parses.
        lint_ok, lint_note = self._lint_generated(CODE_FILE)
        if not lint_ok:
            self.state.record_error(f"build: {lint_note}")
            self.state.save()
            return replace_result(result, build_ok=False, summary=lint_note)
        return replace_result(
            result,
            build_ok=build_ok,
            summary=summary,
        )

    def _persist_package(self, reply: AgentReply) -> dict[str, Any] | None:
        """Write a multi-file artifact tree. ``None`` == not package output.

        Returning ``None`` (no ``### FILE:`` markers, or a failed reply) lets
        ``run_build`` fall back to the single-file path, so enabling package
        mode can never make a previously-working build stop producing code.
        """
        if reply.failed:
            return None
        files = parse_multifile(reply.text, max_files=self.config.execution.max_files_per_build)
        if not files:
            return None

        written: list[str] = []
        for artifact in files:
            try:
                written.append(self.write_file(artifact.path, artifact.content))
            except WorkspaceEscapeError:
                logger.error("Package file escapes workspace, refused: %s", artifact.path)
                self.state.record_error(f"build: refused path {artifact.path}")
                return {
                    "build_ok": False,
                    "summary": f"refused unsafe artifact path {artifact.path}",
                }

        ok, note = verify_python_files(files)
        if not ok:
            self.state.record_error(f"build: {note}")
            self.state.save()
            return {"build_ok": False, "summary": note, "files_created": tuple(written)}

        # The reviewer and security agents read CODE_FILE; give them every
        # module, otherwise vulnerabilities outside the first file go unaudited.
        self.write_file(CODE_FILE, primary_module(files))

        for artifact in files:
            if not artifact.is_python:
                continue
            lint_ok, lint_note = self._lint_generated(artifact.path)
            if not lint_ok:
                self.state.record_error(f"build: {lint_note}")
                self.state.save()
                return {
                    "build_ok": False,
                    "summary": lint_note,
                    "files_created": tuple(written),
                }

        self.state.save()
        return {
            "build_ok": True,
            "summary": f"Package generated: {len(files)} files",
            "files_created": tuple(written),
        }

    async def run_test(self, goal: str) -> PhaseResult:
        code = self.read_file(CODE_FILE)
        reply, result = await self._run_agent_phase(
            phase="test",
            agent_key="tester",
            prompt=self.prompts.test(goal, code),
            output_file=TESTS_FILE,
        )

        if reply.failed:
            return replace_result(result, tests_passed=0, tests_total=0)

        passed, failed, note = await self._execute_generated_tests()
        # Generated suite is clean and its own tests pass -> also verify against
        # the user-owned suite if one is configured (real correctness signal).
        user_passed = user_failed = 0
        if failed == 0 and note == "":
            user_passed, user_failed, user_note = await self._run_user_tests()
            # User-suite results ALWAYS count. Folding them in only when a
            # note was set discarded genuine failures: a suite that ran
            # cleanly and failed 5 of 5 still scored the full test weight,
            # which defeats the whole point of user_tests_dir (ADR-006).
            passed += user_passed
            failed += user_failed
            if user_note:
                note = user_note
        total = passed + failed
        summary = f"Tests: {passed} passed, {failed} failed"
        if user_passed or user_failed:
            summary += f" (user suite: {user_passed} passed, {user_failed} failed)"
        if note:
            summary = f"{summary} ({note})"
            self.state.record_error(f"test: {note}")
        self.state.save()
        return replace_result(
            result,
            ok=True,
            tests_passed=passed,
            tests_total=total,
            summary=summary,
        )

    async def run_review(self, goal: str) -> PhaseResult:
        code = self.read_file(CODE_FILE)
        extra_system = (
            "You are a separate reviewer instance from whoever built this code, "
            "with no stake in it being accepted. Be skeptical and thorough, not "
            "agreeable."
        )
        reply, result = await self._run_agent_phase(
            phase="review",
            agent_key="reviewer",
            prompt=self.prompts.review(goal, code),
            output_file=REVIEW_FILE,
            extra_system=extra_system,
        )

        # A failed reviewer scores 0, never a silent pass.
        review_score = 0.0
        if reply.ok:
            match = REVIEW_SCORE_RE.search(reply.text)
            if match:
                review_score = max(0.0, min(100.0, float(match.group(1))))
            else:
                logger.warning("Review output contained no 'Score: <n>' line; scoring 0")
        else:
            logger.error("Review agent failed; scoring 0 to avoid a false pass")

        return replace_result(
            result,
            review_score=review_score,
            summary=f"Review score: {review_score}",
        )

    async def run_security_audit(self, goal: str) -> PhaseResult:
        code = self.read_file(CODE_FILE)
        reply, result = await self._run_agent_phase(
            phase="security_audit",
            agent_key="security_auditor",
            prompt=self.prompts.security_audit(goal, code),
            output_file=SECURITY_FILE,
        )

        if reply.failed:
            # An agent outage must never read as "no issues found".
            issues = ["CRITICAL: security audit did not complete"]
        else:
            issues = parse_security_findings(reply.text)
            if not issues and not reports_no_issues(reply.text):
                issues = ["MEDIUM: audit output not in expected format"]

        return replace_result(
            result,
            security_issues=tuple(issues),
            summary=f"{len(issues)} security issue(s) found",
        )

    async def run_performance_optimize(self, goal: str) -> PhaseResult:
        code = self.read_file(CODE_FILE)
        # Written to a separate file on purpose: this rewrite has not been
        # through test/review/security, so it must not become canonical.
        reply, result = await self._run_agent_phase(
            phase="performance_optimize",
            agent_key="performance_optimizer",
            prompt=self.prompts.performance_optimize(goal, code),
            output_file=OPTIMIZED_FILE,
        )
        summary = "Performance pass completed" if reply.ok else result.summary
        return replace_result(result, summary=summary)

    async def run_documentation(self, goal: str) -> PhaseResult:
        architecture = self.read_file(DESIGN_FILE)
        code = self.read_file(OPTIMIZED_FILE) or self.read_file(CODE_FILE)
        reply, result = await self._run_agent_phase(
            phase="documentation",
            agent_key="documentation_writer",
            prompt=self.prompts.documentation(goal, architecture, code),
            output_file=DOCS_FILE,
        )
        summary = "Documentation generated" if reply.ok else result.summary
        return replace_result(result, summary=summary)

    async def run_fix(self, goal: str, issues: Sequence[str]) -> PhaseResult:
        code = self.read_file(CODE_FILE)
        cycle = int(self.state.state.get("cycle", 0))
        archive = f"src/fixes_cycle_{cycle}.py"

        reply, result = await self._run_agent_phase(
            phase="fix",
            agent_key="fixer",
            prompt=self.prompts.fix(goal, code, issues),
            output_file=archive,
        )

        files = list(result.files_created)
        build_ok, note = self._verify_syntax(reply)
        if reply.ok:
            if not build_ok:
                # Never overwrite a valid artifact with a broken fix: the next
                # phase imports CODE_FILE, and the reviewer/security agents
                # audit it. A fix that does not parse must lose, not corrupt.
                self.state.record_error(f"fix: {note}")
                self.state.save()
                return replace_result(
                    result,
                    build_ok=False,
                    files_created=tuple(files),
                    summary=note or result.summary,
                )
            # Persist the fence-stripped source, exactly as run_build does.
            # Writing reply.text raw left ```python fences on disk, so
            # build_ok=True described a file that was not valid Python.
            files.append(self.write_file(CODE_FILE, strip_code_fences(reply.text)))
            lint_ok, lint_note = self._lint_generated(CODE_FILE)
            if not lint_ok:
                self.state.record_error(f"fix: {lint_note}")
                self.state.save()
                return replace_result(
                    result,
                    build_ok=False,
                    files_created=tuple(files),
                    summary=lint_note,
                )
            self.state.save()

        return replace_result(
            result,
            build_ok=build_ok,
            files_created=tuple(files),
            summary=("Fixes applied" if build_ok else (note or result.summary)),
        )
