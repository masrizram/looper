"""Execution of untrusted, LLM-authored code: syntax, lint, and pytest.

Everything here spawns a subprocess or parses generated source. Keeping it
apart from the agent orchestration makes the trust boundary explicit: this
module is where code the AI wrote actually runs (ADR-006, ADR-008).
"""

from __future__ import annotations

import ast
import asyncio
import logging
import subprocess  # nosec B404 - used with a fixed argv, never shell=True
import sys
from pathlib import Path

from looper.adequacy import evaluate_suite
from looper.config import LooperConfig
from looper.llm import AgentReply
from looper.phases.workspace import strip_code_fences
from looper.sandbox import SandboxUnavailableError, run_sandboxed, scan_for_dangerous_calls
from looper.state import StateManager
from looper.testparse import parse_test_summary

logger = logging.getLogger("looper.phases")


class ExecutionMixin:
    """Syntax verification, the lint gate, and sandboxed pytest runs."""

    config: LooperConfig
    state: StateManager
    workspace: Path
    _config_dir: Path | None

    def resolve_in_workspace(self, relative_path: str) -> Path:  # pragma: no cover - mixin contract
        raise NotImplementedError

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

    async def _execute_generated_tests(self) -> tuple[int, int, str]:
        """Run the generated suite, refusing/sandboxing untrusted code.

        Hardened against LLM-authored code in three layers:
          * static scan -- if the suite calls ``os.system``/``subprocess``/
            network/socket/eval etc., we refuse to run it *at all* (the cost
            of one destructive line far outweighs any signal it could give);
          * adequacy gate -- a suite of a single ``assert True`` is rejected so
            the build cannot go green on a test written to pass;
          * execution -- if it passes both gates it runs in a fixed-argv
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
            subject_modules=self._subject_modules(),
        )
        if not report.ok:
            logger.error("Generated test suite inadequate: %s", report.reason)
            return 0, 1, f"test suite inadequate: {report.reason}"

        return await self._run_pytest(tests_dir)

    def _subject_modules(self) -> frozenset[str]:
        """Top-level module names the build actually wrote into ``src/``.

        Handing these to the adequacy gate is what makes "does this suite test
        anything?" an exact question. Without them a tautological suite passed
        by importing ``logging``. An empty result (no src tree yet) makes the
        gate fall back to its stdlib-denylist behaviour rather than refusing
        every suite.
        """
        src = self.workspace / "src"
        try:
            entries = list(src.iterdir())
        except OSError:
            return frozenset()
        names = {
            entry.stem
            for entry in entries
            if entry.is_file() and entry.suffix == ".py" and not entry.name.startswith("_")
        }
        names |= {
            entry.name for entry in entries if entry.is_dir() and not entry.name.startswith("_")
        }
        return frozenset(names)

    async def _run_user_tests(self) -> tuple[int, int, str]:
        """Run the user-owned suite (if configured) against the generated code.

        The AI cannot see or edit these tests, so passing them is real evidence
        the generated code works -- this closes the self-test overfitting hole.
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
        # In container mode only the workspace is bind-mounted at /work, so a
        # user suite living outside it is simply not present in the image.
        # Running it there would silently test nothing; say so instead.
        exec_cfg = self.config.execution
        if exec_cfg.sandbox_tests and exec_cfg.sandbox_backend in ("auto", "docker", "podman"):
            try:
                user_path.resolve().relative_to(self.workspace.resolve())
            except ValueError:
                logger.warning(
                    "user_tests_dir %s is outside the workspace and is not mounted "
                    "into the sandbox container; skipping it rather than reporting "
                    "an unverified pass",
                    user_path,
                )
                return 0, 0, ""
        return await self._run_pytest(user_path)

    async def _run_pytest(self, tests_dir: Path) -> tuple[int, int, str]:
        """Launch pytest for ``tests_dir`` with sandboxing where available.

        Isolation is ``-E -s`` rather than ``-I``. ``-I`` implies ``-P``,
        which drops the script directory from ``sys.path`` -- so every suite
        that did the one thing the test prompt demands ("import from the
        module under test") failed collection with ``ModuleNotFoundError``,
        scoring 0 tests for reasons that had nothing to do with the code.
        ``-E`` (ignore PYTHON* env vars) and ``-s`` (ignore user site-packages)
        keep the isolation that actually mattered.
        """
        exec_cfg = self.config.execution
        argv = [
            sys.executable,
            "-E",
            "-s",
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
                    cpu_shares=exec_cfg.sandbox_cpu_shares,
                    wall_seconds=exec_cfg.sandbox_wall_seconds,
                    rss_bytes=exec_cfg.sandbox_rss_bytes,
                    backend=exec_cfg.sandbox_backend,
                    image=exec_cfg.sandbox_image,
                    network=exec_cfg.sandbox_network,
                    fail_closed=exec_cfg.sandbox_fail_closed,
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
        except SandboxUnavailableError as exc:
            # Fail closed: no isolation means the suite does not run, and the
            # build loses the test weight rather than gaining it unverified.
            logger.error("Refusing to run generated tests unsandboxed: %s", exc)
            return 0, 1, f"sandbox unavailable: {exc}"
        except OSError as exc:
            logger.exception("Could not spawn the test subprocess")
            return 0, 1, f"could not run tests: {exc}"
        except Exception as exc:  # noqa: BLE001 - a broken backend must not kill the build
            # Anything else (a malformed sandbox argv, a runtime raising from
            # the worker thread) previously propagated out of run_test and
            # aborted the whole build. The test phase failing closed is the
            # correct blast radius.
            logger.exception("Test execution failed unexpectedly")
            return 0, 1, f"test execution error: {exc}"

        passed, failed = parse_test_summary(proc.stdout, proc.stderr)
        if passed == 0 and failed == 0 and proc.returncode != 0:
            return 0, 1, f"pytest exited {proc.returncode} with no test summary"
        return passed, failed, ""
