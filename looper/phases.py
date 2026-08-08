"""Phase execution: one method per pipeline stage, sharing one template."""

from __future__ import annotations

import ast
import asyncio
import logging
import re
import subprocess  # nosec B404 - used with a fixed argv, never shell=True
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from looper.adequacy import evaluate_suite
from looper.config import LooperConfig
from looper.llm import AgentReply, OpenRouterClient
from looper.prompts import PromptGenerator
from looper.sandbox import run_sandboxed, scan_for_dangerous_calls
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

#: Agents habitually wrap code in ```python fences, and often prefix the
#: block with prose ("Here is the code:"). Parsing the fenced text as Python
#: would always raise, so the first fenced block's body is extracted; if there
#: is no fence the text is returned unchanged (the builder may emit bare code).
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def strip_code_fences(text: str) -> str:
    """Return the body of the first fenced block, or ``text`` unchanged.

    A non-anchored search (not ``match``) is deliberate: a reply such as
    "Here is the code:\\n```python\\nx = 1\\n```" must yield ``x = 1``, not the
    whole fenced string, otherwise ``ast.parse`` rejects valid code and the
    build fails closed for no reason.
    """
    match = _FENCE_RE.search(text or "")
    return match.group(1).strip() if match else (text or "")


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

    def invalidate_unverified(self, phases: Sequence[str]) -> None:
        """Drop evidence no phase in this cycle will re-establish.

        Carrying a previous cycle's review score or empty findings list into a
        cycle that does not re-run those phases scores unverified facts as
        verified. A trimmed ``retry_phases`` could therefore keep banking a 98
        review from cycle 1 forever.
        """
        if "review" not in phases:
            self.review_score = 0.0
        if "security_audit" not in phases:
            self.security_issues = ["MEDIUM: security audit not re-run this cycle"]
        if "test" not in phases:
            self.tests_passed = 0
            self.tests_total = 0


class PhaseManager:
    """Runs individual pipeline phases against a workspace."""

    def __init__(
        self,
        config: LooperConfig,
        state: StateManager,
        client: OpenRouterClient,
        *,
        prompts: PromptGenerator | None = None,
        config_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.client = client
        self.prompts = prompts or PromptGenerator()
        self.workspace = Path(config.workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        # Directory of the active config file, used to resolve a relative
        # user_tests_dir without guessing the daemon's CWD.
        self._config_dir = Path(config_dir).resolve() if config_dir else None

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

    def _lint_generated(self, relative_path: str) -> tuple[bool, str]:
        """Compile/lint generated code before it is accepted.

        ``py_compile`` catches latent syntax/indent errors ``ast.parse`` can
        miss; ``flake8`` adds style checks. Returns (ok, note).
        """
        mode = self.config.execution.lint_generated
        if mode == "off":
            return True, ""
        path = self.resolve_in_workspace(relative_path)
        if mode == "py_compile":
            try:
                subprocess.run(  # nosec B603 - fixed argv, no shell
                    [sys.executable, "-m", "py_compile", str(path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                return False, f"generated code failed py_compile: {exc.stderr[:200]}"
            return True, ""
        # Only the flake8 path remains: off/py_compile already returned above,
        # and build_config guarantees the mode is one of off|py_compile|flake8.
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-m", "flake8", "--max-line-length=100", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return False, f"generated code failed flake8: {proc.stdout[:200]}"
        return True, ""

    def _verify_syntax(self, reply: AgentReply) -> tuple[bool, str]:
        """``build_ok`` must mean *the code parses*, not *the LLM answered*.

        Previously any non-empty reply -- prose, an apology, a truncated file --
        scored the full build weight. Now the generated module is parsed; a
        syntax error fails the build closed.
        """
        if reply.failed:
            return False, ""
        source = strip_code_fences(reply.text)
        if not source.strip():
            return False, "build produced empty output"
        try:
            ast.parse(source)
        except SyntaxError as exc:
            logger.error("Generated code does not parse: %s", exc)
            return False, f"generated code has a syntax error: {exc}"
        return True, ""

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
            if user_note:
                note = user_note
                failed += user_failed
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

    async def _execute_generated_tests(self) -> tuple[int, int, str]:
        """Run the generated suite, refusing/sandboxing untrusted code.

        Hardened against LLM-authored code in three layers:
          * static scan — if the suite calls ``os.system``/``subprocess``/
            network/socket/eval etc., we refuse to run it *at all* (the cost
            of one destructive line far outweighs any signal it could give);
          * adequacy gate — a suite of a single ``assert True`` is rejected so
            the build cannot go green on a test written to pass;
          * execution — if it passes both gates it runs in a fixed-argv
            subprocess under OS resource limits so a runaway loop or memory
            hog is killed instead of wedging the daemon.
        """
        tests_dir = self.workspace / "tests"
        test_src = ""
        try:
            test_src = (tests_dir / "test_generated.py").read_text(encoding="utf-8")
        except OSError:
            return 0, 1, "test file missing"

        dangerous = scan_for_dangerous_calls(test_src)
        if dangerous:
            logger.error("Refusing to run generated suite: %s", "; ".join(dangerous))
            return 0, 1, f"dangerous call in suite refused: {dangerous[0]}"

        report = evaluate_suite(
            test_src,
            min_assertions_per_100_lines=self.config.execution.min_test_assertions_per_100_lines,
        )
        if not report.ok:
            logger.error("Generated test suite inadequate: %s", report.reason)
            return 0, 1, f"test suite inadequate: {report.reason}"

        return await self._run_pytest(tests_dir)

    async def _run_user_tests(self) -> tuple[int, int, str]:
        """Run the user-owned suite (if configured) against the generated code.

        The AI cannot see or edit these tests, so passing them is real evidence
        the generated code works — this closes the self-test overfitting hole.
        """
        user_dir = self.config.execution.user_tests_dir
        if not user_dir:
            return 0, 0, ""
        user_path = Path(user_dir)
        if not user_path.is_absolute():
            user_path = (self._config_dir / user_path) if self._config_dir else user_path
        if not user_path.exists():
            logger.warning("user_tests_dir %s does not exist; skipping", user_path)
            return 0, 0, ""
        return await self._run_pytest(user_path)

    async def _run_pytest(self, tests_dir: Path) -> tuple[int, int, str]:
        """Launch pytest for ``tests_dir`` with sandboxing where available."""
        exec_cfg = self.config.execution
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
        timeout = exec_cfg.test_timeout_seconds
        try:
            if exec_cfg.sandbox_tests:
                proc = await asyncio.to_thread(
                    run_sandboxed,  # nosec B603 - fixed argv, no shell
                    argv,
                    cwd=str(self.workspace),
                    timeout=timeout,
                    cpu_seconds=exec_cfg.sandbox_cpu_seconds,
                    wall_seconds=exec_cfg.sandbox_wall_seconds,
                    rss_bytes=exec_cfg.sandbox_rss_bytes,
                )
            else:
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
            logger.error("Test suite timed out after %ss", timeout)
            return 0, 1, f"timed out after {timeout}s"
        except OSError as exc:
            logger.exception("Could not spawn the test subprocess")
            return 0, 1, f"could not run tests: {exc}"

        passed, failed = parse_test_summary(proc.stdout, proc.stderr)
        if passed == 0 and failed == 0 and proc.returncode != 0:
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
        build_ok, note = self._verify_syntax(reply)
        if reply.ok:
            files.append(self.write_file(CODE_FILE, reply.text))
            if not build_ok:
                self.state.record_error(f"fix: {note}")
            self.state.save()

        return replace_result(
            result,
            build_ok=build_ok,
            files_created=tuple(files),
            summary=("Fixes applied" if build_ok else (note or result.summary)),
        )


def replace_result(result: PhaseResult, **changes: Any) -> PhaseResult:
    """``dataclasses.replace`` for :class:`PhaseResult` (frozen + slots)."""
    import dataclasses

    return dataclasses.replace(result, **changes)
