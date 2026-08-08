"""Phase execution: one method per pipeline stage, sharing one template."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess  # nosec B404 - used with a fixed argv, never shell=True
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from looper.config import LooperConfig
from looper.llm import AgentReply, OpenRouterClient
from looper.prompts import PromptGenerator
from looper.scoring import NO_ISSUES_MARKERS, parse_security_findings
from looper.state import StateManager
from looper.testparse import parse_test_summary

logger = logging.getLogger("looper.phases")

REVIEW_SCORE_RE = re.compile(r"score\s*[:=]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)

RESEARCH_FILE = "research.md"
DESIGN_FILE = "architecture/design.md"
CODE_FILE = "src/generated_code.py"
OPTIMIZED_FILE = "src/optimized_code.py"
TESTS_FILE = "tests/test_generated.py"
REVIEW_FILE = "review.md"
SECURITY_FILE = "security_audit.md"
DOCS_FILE = "docs/README.md"


class WorkspaceEscapeError(ValueError):
    """Raised when a target path would resolve outside the workspace."""


@dataclass(frozen=True, slots=True)
class PhaseResult:
    """Structured outcome of a phase.

    Every risk-bearing field defaults to the *pessimistic* value. The previous
    dict-based contract used ``result.get("build_ok", True)``, so a phase that
    forgot the key was silently treated as a success.
    """

    phase: str
    agent: str
    model: str
    ok: bool = False
    summary: str = ""
    files_created: tuple[str, ...] = ()
    build_ok: bool = False
    tests_passed: int = 0
    tests_total: int = 0
    review_score: float = 0.0
    security_issues: tuple[str, ...] = ()
    error: str = ""

    @property
    def status(self) -> str:
        return "done" if self.ok else "error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "status": self.status,
            "agent": self.agent,
            "model": self.model,
            "summary": self.summary,
            "files_created": list(self.files_created),
            "build_ok": self.build_ok,
            "tests_passed": self.tests_passed,
            "tests_total": self.tests_total,
            "review_score": self.review_score,
            "security_issues": list(self.security_issues),
            "error": self.error,
        }


@dataclass
class CycleEvidence:
    """Accumulated facts for one cycle, fed to the scoring engine."""

    build_ok: bool = False
    tests_passed: int = 0
    tests_total: int = 0
    review_score: float = 0.0
    security_issues: list[str] = field(default_factory=list)

    def absorb(self, result: PhaseResult) -> None:
        if result.phase in ("build", "fix"):
            self.build_ok = result.build_ok
        elif result.phase == "test":
            self.tests_passed = result.tests_passed
            self.tests_total = result.tests_total
        elif result.phase == "review":
            self.review_score = result.review_score
        elif result.phase == "security_audit":
            self.security_issues = list(result.security_issues)


class PhaseManager:
    """Runs individual pipeline phases against a workspace."""

    def __init__(
        self,
        config: LooperConfig,
        state: StateManager,
        client: OpenRouterClient,
        *,
        prompts: PromptGenerator | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.client = client
        self.prompts = prompts or PromptGenerator()
        self.workspace = Path(config.workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)

    # -- Workspace I/O ---------------------------------------------------

    def resolve_in_workspace(self, relative_path: str) -> Path:
        """Resolve ``relative_path`` inside the workspace, or refuse.

        This is the single filesystem sink for LLM-influenced names, so the
        containment check belongs here rather than at each call site.
        """
        root = self.workspace.resolve()
        candidate = (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise WorkspaceEscapeError(f"Path escapes workspace: {relative_path!r}")
        return candidate

    def write_file(self, relative_path: str, content: str) -> str:
        """Write agent output into the workspace, size-capped.

        Content is truncated rather than rejected: a partial artifact is more
        useful to the next phase than none, and the marker makes the
        truncation obvious to both the reviewer agent and a human.
        """
        path = self.resolve_in_workspace(relative_path)
        limit = self.config.execution.max_file_bytes
        encoded = content.encode("utf-8")
        if len(encoded) > limit:
            logger.warning(
                "Agent output for %s is %d bytes, over the %d byte cap; truncating",
                relative_path,
                len(encoded),
                limit,
            )
            content = encoded[:limit].decode("utf-8", errors="ignore")
            content += f"\n\n# [TRUNCATED by looper at {limit} bytes]\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.state.record_files([str(path)])
        return str(path)

    def read_file(self, relative_path: str) -> str:
        path = self.resolve_in_workspace(relative_path)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

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

        result = PhaseResult(
            phase=phase,
            agent=agent.role,
            model=agent.model,
            ok=reply.ok,
            summary="" if reply.ok else f"{agent.role} failed: {reply.error}",
            files_created=(written,),
            error=reply.error,
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
        reply, result = await self._run_agent_phase(
            phase="build",
            agent_key="builder",
            prompt=self.prompts.build(goal, architecture),
            output_file=CODE_FILE,
        )
        return replace_result(
            result,
            build_ok=reply.ok,
            summary="Code generated" if reply.ok else result.summary,
        )

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
        total = passed + failed
        summary = f"Tests: {passed} passed, {failed} failed"
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

    async def _execute_generated_tests(self) -> tuple[int, int, str]:
        """Run the generated suite in an isolated subprocess.

        Hardened against LLM-authored code: fixed argv (never ``shell=True``),
        ``-I`` isolated mode, no cache writes, and a hard timeout so one
        ``while True:`` cannot wedge a 24/7 daemon forever.
        """
        tests_dir = self.workspace / "tests"
        timeout = self.config.execution.test_timeout_seconds
        argv = [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "pytest",
            str(tests_dir),
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-header",
        ]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,  # nosec B603 - fixed argv, no shell
                argv,
                capture_output=True,
                text=True,
                cwd=str(self.workspace),
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.error("Generated test suite timed out after %ss", timeout)
            return 0, 1, f"timed out after {timeout}s"
        except OSError as exc:
            logger.exception("Could not spawn the test subprocess")
            return 0, 1, f"could not run tests: {exc}"

        passed, failed = parse_test_summary(proc.stdout, proc.stderr)
        if passed == 0 and failed == 0 and proc.returncode != 0:
            # Non-zero exit with no parsable summary == collection error.
            return 0, 1, f"pytest exited {proc.returncode} with no test summary"
        return passed, failed, ""

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
            lowered = reply.text.lower()
            if not issues and not any(marker in lowered for marker in NO_ISSUES_MARKERS):
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
        if reply.ok:
            files.append(self.write_file(CODE_FILE, reply.text))
            self.state.save()

        return replace_result(
            result,
            build_ok=reply.ok,
            files_created=tuple(files),
            summary="Fixes applied" if reply.ok else result.summary,
        )


def replace_result(result: PhaseResult, **changes: Any) -> PhaseResult:
    """``dataclasses.replace`` for :class:`PhaseResult` (frozen + slots)."""
    import dataclasses

    return dataclasses.replace(result, **changes)
