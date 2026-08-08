#!/usr/bin/env python3
"""Looper Daemon - Autonomous AI Software Engineering System.

Jalan 24/7 di background, auto-build project dari 1 goal.

Semua agent (kecuali Orchestrator) dipanggil lewat OpenRouter
(https://openrouter.ai/api/v1), satu API key untuk banyak model.
Role -> model mengikuti tabel "ROLE & MODEL ASSIGNMENT":

    Orchestrator            Looper (daemon ini sendiri, tanpa LLM)
    System Architect         deepseek/deepseek-r1
    UX/API Designer          openai/gpt-4o
    Code Builder             anthropic/claude-3.5-sonnet
    Test Generator           deepseek/deepseek-r1
    Senior Reviewer          anthropic/claude-3.5-sonnet  (instance terpisah)
    Security Auditor         openai/gpt-4o
    Performance Optimizer    anthropic/claude-3.5-sonnet
    Documentation Writer     google/gemini-pro-1.5

Catatan: "Researcher" tidak ada di tabel aslinya, jadi phase riset
dipetakan ke model yang sama dengan System Architect (deepseek-r1)
karena satu rumpun tugas (breakdown requirements). Ubah lewat config
kalau mau agent lain.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

try:
    import openai
except ImportError:
    openai = None

try:
    from aiohttp import web
except ImportError:
    web = None

logger = logging.getLogger("looper")


# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_CONFIG_FILENAMES = ("config.yaml", "looper_config.yaml")


def load_config(path: str | None = None) -> dict:
    """Load YAML config.

    If ``path`` is not given, try the known default filenames in order and
    raise a clear error only if none exist. This fixes the previous crash
    where the loader looked for ``looper_config.yaml`` while the file on disk
    is ``config.yaml``.
    """
    candidates = [path] if path else list(DEFAULT_CONFIG_FILENAMES)
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                logger.info("Loading config from %s", candidate)
                return validate_config(yaml.safe_load(f) or {})
        except FileNotFoundError as exc:
            last_error = exc
            continue
    if path:
        raise FileNotFoundError(f"Config file not found: {path}")
    raise FileNotFoundError(
        "No config file found. Looked for: "
        + ", ".join(DEFAULT_CONFIG_FILENAMES)
        + f" (last error: {last_error})"
    )


def validate_config(raw: dict) -> dict:
    """Validate and normalise the raw config dict.

    Guards against mis-typed values that would otherwise fail silently at
    runtime (e.g. ``http_port`` as a string, ``max_cycles`` <= 0).
    """
    if not isinstance(raw, dict):
        raise ValueError("Config root must be a mapping")

    execution = raw.get("execution", {}) or {}
    max_cycles = execution.get("max_cycles", 5)
    if not isinstance(max_cycles, int) or max_cycles <= 0:
        raise ValueError(f"execution.max_cycles must be a positive int, got {max_cycles!r}")

    http_port = raw.get("http_port", 9999)
    if not isinstance(http_port, int) or not (1 <= http_port <= 65535):
        raise ValueError(f"http_port must be an int 1-65535, got {http_port!r}")

    target_score = execution.get("target_score", 99)
    min_acceptable = execution.get("min_acceptable", 95)
    if not (0 <= min_acceptable <= target_score <= 100):
        raise ValueError(
            "Need 0 <= min_acceptable <= target_score <= 100, "
            f"got min={min_acceptable}, target={target_score}"
        )

    http = raw.get("http", {}) or {}
    bind = http.get("bind", "127.0.0.1")
    if bind not in ("127.0.0.1", "0.0.0.0", "localhost"):
        logger.warning("Unusual http.bind=%r; expecting 127.0.0.1 or 0.0.0.0", bind)

    return raw


# Module-level runtime config. Populated by configure(); safe to monkeypatch
# in tests via daemon.configure(test_dict).
CONFIG = {}
WORKSPACE = Path("./workspace")
STATE_FILE = Path("./looper_state.json")
WATCH_FILE = Path("./looper_commands.txt")
HTTP_PORT = 9999
HTTP_BIND = "127.0.0.1"
HTTP_AUTH_TOKEN = ""
MAX_CYCLES = 5
TARGET_SCORE = 99
MIN_ACCEPTABLE = 95
FIRST_CYCLE_PHASES = ["research", "architecture", "build", "test", "review", "security_audit"]
RETRY_CYCLE_PHASES = ["test", "review", "security_audit"]
FINAL_PHASES = ["performance_optimize", "documentation"]
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_SITE_URL = ""
OPENROUTER_SITE_NAME = "Looper Daemon"
AGENTS = {}

DEFAULT_AGENTS = {
    "researcher": {
        "model": "deepseek/deepseek-r1",
        "role": "Senior Technical Researcher",
        "temperature": 0.3,
        "max_tokens": 8192,
    },
    "architect": {
        "model": "deepseek/deepseek-r1",
        "role": "System Architect",
        "temperature": 0.3,
        "max_tokens": 8192,
    },
    "ux_api_designer": {
        "model": "openai/gpt-4o",
        "role": "UX/API Designer",
        "temperature": 0.4,
        "max_tokens": 8192,
    },
    "builder": {
        "model": "anthropic/claude-3.5-sonnet",
        "role": "Code Builder",
        "temperature": 0.2,
        "max_tokens": 8192,
    },
    "tester": {
        "model": "deepseek/deepseek-r1",
        "role": "Test Generator",
        "temperature": 0.3,
        "max_tokens": 8192,
    },
    "reviewer": {
        "model": "anthropic/claude-3.5-sonnet",
        "role": "Senior Reviewer",
        "temperature": 0.2,
        "max_tokens": 8192,
    },
    "security_auditor": {
        "model": "openai/gpt-4o",
        "role": "Security Auditor",
        "temperature": 0.2,
        "max_tokens": 8192,
    },
    "performance_optimizer": {
        "model": "anthropic/claude-3.5-sonnet",
        "role": "Performance Optimizer",
        "temperature": 0.2,
        "max_tokens": 8192,
    },
    "documentation_writer": {
        "model": "google/gemini-pro-1.5",
        "role": "Documentation Writer",
        "temperature": 0.4,
        "max_tokens": 8192,
    },
    "fixer": {
        "model": "anthropic/claude-3.5-sonnet",
        "role": "Expert Fixer",
        "temperature": 0.2,
        "max_tokens": 8192,
    },
}


def configure(raw: dict | None = None) -> dict:
    """Derive module-level runtime globals from a config dict.

    Returns the (validated) config so callers can assert on it.
    """
    global CONFIG, WORKSPACE, STATE_FILE, WATCH_FILE, HTTP_PORT, HTTP_BIND
    global HTTP_AUTH_TOKEN, MAX_CYCLES, TARGET_SCORE, MIN_ACCEPTABLE
    global FIRST_CYCLE_PHASES, RETRY_CYCLE_PHASES, FINAL_PHASES
    global OPENROUTER_BASE_URL, OPENROUTER_API_KEY_ENV, OPENROUTER_SITE_URL
    global OPENROUTER_SITE_NAME, AGENTS

    if raw is None:
        raw = load_config()
    else:
        raw = validate_config(raw)
    CONFIG = raw

    WORKSPACE = Path(raw.get("workspace", "./workspace"))
    STATE_FILE = Path(raw.get("state_file", "./looper_state.json"))
    WATCH_FILE = Path(raw.get("watch_file", "./looper_commands.txt"))

    http = raw.get("http", {}) or {}
    HTTP_BIND = http.get("bind", "127.0.0.1")
    HTTP_PORT = int(http.get("port", raw.get("http_port", 9999)))
    auth_env = http.get("auth_token_env", "LOOPER_HTTP_TOKEN")
    HTTP_AUTH_TOKEN = os.environ.get(auth_env, "") or ""

    execution = raw.get("execution", {}) or {}
    MAX_CYCLES = int(execution.get("max_cycles", 5))
    TARGET_SCORE = float(execution.get("target_score", 99))
    MIN_ACCEPTABLE = float(execution.get("min_acceptable", 95))

    FIRST_CYCLE_PHASES = raw.get(
        "phases", ["research", "architecture", "build", "test", "review", "security_audit"]
    )
    RETRY_CYCLE_PHASES = raw.get("retry_phases", ["test", "review", "security_audit"])
    FINAL_PHASES = raw.get("final_phases", ["performance_optimize", "documentation"])

    or_cfg = raw.get("openrouter", {}) or {}
    OPENROUTER_BASE_URL = or_cfg.get("base_url", "https://openrouter.ai/api/v1")
    OPENROUTER_API_KEY_ENV = or_cfg.get("api_key_env", "OPENROUTER_API_KEY")
    OPENROUTER_SITE_URL = or_cfg.get("site_url", "")
    OPENROUTER_SITE_NAME = or_cfg.get("site_name", "Looper Daemon")

    overrides = raw.get("agents", {}) or {}
    AGENTS = {
        key: {**defaults, **overrides.get(key, {})} for key, defaults in DEFAULT_AGENTS.items()
    }

    if HTTP_AUTH_TOKEN:
        logger.info("HTTP API auth enabled (token from env %s)", auth_env)
    else:
        logger.warning(
            "HTTP API has NO auth token set (env %s). "
            "Bind is %s; only expose on trusted networks / behind a reverse proxy.",
            auth_env,
            HTTP_BIND,
        )
    return raw


configure()


# ============================================================================
# STATE MANAGER
# ============================================================================

DEFAULT_STATE = {
    "current_goal": None,
    "current_phase": "idle",
    "cycle": 0,
    "score": 0.0,
    "status": "idle",
    "history": [],
    "files_created": [],
    "errors": [],
}


class StateManager:
    """Persists daemon state as JSON.

    ``update()`` mutates the in-memory dict only; call ``save()`` explicitly
    to flush to disk. This avoids re-writing the whole file on every small
    change (the previous O(n^2) I/O pattern).
    """

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.state = self._load()

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {**DEFAULT_STATE, **data}
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Corrupt state file %s: %s", self.state_file, exc)
        return dict(DEFAULT_STATE)

    def save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
        tmp.replace(self.state_file)

    def update(self, **kwargs):
        self.state.update(kwargs)

    def reset(self):
        self.state = dict(DEFAULT_STATE)
        self.save()


# ============================================================================
# SCORING ENGINE
# ============================================================================


class ScoringEngine:
    @staticmethod
    def calculate_score(
        build_ok: bool,
        tests_passed: int,
        tests_total: int,
        security_issues: list,
        review_score: float,
    ) -> float:
        score = 0.0

        if build_ok:
            score += 20.0

        if tests_total > 0:
            score += (tests_passed / tests_total) * 30.0

        security_penalty = len(security_issues) * 5
        score += max(0.0, 30.0 - security_penalty)

        score += max(0.0, min(20.0, review_score * 0.2))

        return min(100.0, score)


# ============================================================================
# TEST RESULT PARSER
# ============================================================================


def parse_test_summary(stdout: str, stderr: str = "") -> tuple[int, int]:
    """Parse pytest CLI output for passed/failed counts.

    Uses the summary line (``N passed``, ``M failed``) rather than counting
    per-line ``PASSED``/``FAILED`` markers, which previously double-counted
    and mis-handled tests whose names contain those words.
    """
    text = (stdout or "") + "\n" + (stderr or "")
    passed = 0
    failed = 0
    for count, label in re.findall(r"(\d+)\s+(passed|failed)", text, re.IGNORECASE):
        if label.lower() == "passed":
            passed += int(count)
        else:
            failed += int(count)
    # Fallback: honour the all-or-nothing summary forms.
    if passed == 0 and failed == 0:
        if re.search(r"\bok\b", text, re.IGNORECASE) and "failed" not in text.lower():
            passed = 1
    return passed, failed


# ============================================================================
# PROMPT GENERATOR
# ============================================================================


class PromptGenerator:
    @staticmethod
    def research(goal: str) -> str:
        return (
            "You are a senior technical researcher. Research the best practices, "
            "tech stack, libraries, and architecture for this project:\n\n"
            f"{goal}\n\n"
            "Provide: recommended stack, rationale, alternatives, and pitfalls. "
            "Output as structured markdown."
        )

    @staticmethod
    def architecture(goal: str, research: str) -> str:
        return (
            "You are a system architect. Design a complete architecture for:\n\n"
            f"{goal}\n\n"
            f"Based on this research:\n{research}\n\n"
            "Provide: components, data models, API design, security, deployment, "
            "and folder structure. Output as structured markdown."
        )

    @staticmethod
    def api_design(goal: str, architecture: str) -> str:
        return (
            "You are a UX/API designer. Given this system architecture for:\n\n"
            f"{goal}\n\n"
            f"Architecture:\n{architecture}\n\n"
            "Propose intuitive API endpoints (or CLI/UX flow if there's no API), "
            "naming conventions, request/response shapes, and error-handling "
            "patterns that maximize developer experience. Output as structured markdown."
        )

    @staticmethod
    def build(goal: str, architecture: str) -> str:
        return (
            "You are an expert code builder. Generate ALL production-ready code "
            f"for:\n\n{goal}\n\n"
            f"Based on this architecture:\n{architecture}\n\n"
            "Rules: NO placeholders, NO TODOs, NO incomplete code. "
            "Every file must be complete and functional. Include tests. "
            "Output each file in a separate code block with filename header."
        )

    @staticmethod
    def test(goal: str, code: str) -> str:
        return (
            "You are a QA engineer. Write comprehensive unit and integration tests "
            f"for:\n\n{goal}\n\n"
            f"Code under test:\n{code}\n\n"
            "Cover: edge cases, error handling, happy paths. Output pytest code "
            "only, importing from the module under test."
        )

    @staticmethod
    def review(goal: str, code: str) -> str:
        return (
            "You are a senior code reviewer, a separate instance from whoever "
            "wrote this code. Review this code for:\n\n"
            f"{goal}\n\n"
            f"Code:\n{code}\n\n"
            "Check: security vulnerabilities, performance issues, code quality, "
            "best practices, missing tests. Provide a severity rating "
            "(critical/high/medium/low) per finding, and end with a line in the "
            "exact format 'Score: <0-100>'."
        )

    @staticmethod
    def security_audit(goal: str, code: str) -> str:
        return (
            "You are a security auditor. Audit this code for:\n\n"
            f"{goal}\n\n"
            f"Code:\n{code}\n\n"
            "Look for injection flaws, auth/authorization issues, secrets handling, "
            "input validation gaps, and other vulnerabilities. List each finding as "
            "its own bullet starting with the severity, e.g. '- HIGH: <description>'. "
            "If there are no issues, say 'No issues found.'"
        )

    @staticmethod
    def performance_optimize(goal: str, code: str) -> str:
        return (
            "You are a performance optimizer. Analyze and refactor this code "
            f"for:\n\n{goal}\n\n"
            f"Code:\n{code}\n\n"
            "Identify time/space complexity issues, unnecessary work, and resource "
            "waste. Provide the optimized code plus a short summary of what changed "
            "and why."
        )

    @staticmethod
    def documentation(goal: str, architecture: str, code: str) -> str:
        return (
            "You are a technical documentation writer. Write documentation "
            f"for:\n\n{goal}\n\n"
            f"Architecture:\n{architecture}\n\n"
            f"Code:\n{code}\n\n"
            "Produce a README with: overview, setup, usage, API/CLI reference, and "
            "configuration. Output as markdown."
        )

    @staticmethod
    def fix(goal: str, code: str, issues: list) -> str:
        issues_text = "\n".join([f"- {i}" for i in issues])
        return (
            "You are an expert fixer. Fix these issues in the project:\n\n"
            f"{goal}\n\n"
            f"Current code:\n{code}\n\n"
            f"Issues to fix:\n{issues_text}\n\n"
            "Provide the corrected, complete code for each affected file."
        )


# ============================================================================
# FILE WATCHER
# ============================================================================


class FileWatcher:
    def __init__(self, watch_file: Path, callback, interval: float = 2.0):
        self.watch_file = watch_file
        self.callback = callback
        self.interval = interval
        self._last_content = ""
        self._running = False

    async def start(self):
        self._running = True
        self.watch_file.parent.mkdir(parents=True, exist_ok=True)
        self.watch_file.touch(exist_ok=True)
        while self._running:
            try:
                content = self.watch_file.read_text(encoding="utf-8")
                if content and content != self._last_content:
                    self._last_content = content
                    await self.callback(content.strip())
            except Exception:  # noqa: BLE001 - watcher must never die
                logger.exception("Watcher error")
            await asyncio.sleep(self.interval)

    def stop(self):
        self._running = False


# ============================================================================
# HTTP SERVER
# ============================================================================


class HTTPServer:
    def __init__(self, port: int, bind: str, callback, auth_token: str = ""):
        self.port = port
        self.bind = bind
        self.callback = callback
        self.auth_token = auth_token
        self._server = None

    async def start(self):
        if web is None:
            raise RuntimeError(
                "The 'aiohttp' package is required for the HTTP server. "
                "Install it with: pip install aiohttp"
            )
        app = web.Application()
        app.add_routes([web.post("/build", self._handle_build)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.bind, self.port)
        await site.start()
        self._server = runner
        logger.info("HTTP server listening on %s:%s", self.bind, self.port)

    async def _handle_build(self, request):
        if self.auth_token:
            header = request.headers.get("Authorization", "")
            if header != f"Bearer {self.auth_token}":
                return web.json_response({"error": "unauthorized"}, status=401)
        try:
            data = await request.json()
            goal = (data.get("goal") or "").strip()
            if not goal:
                return web.json_response({"error": "goal required"}, status=400)
            task = asyncio.create_task(self.callback(goal))
            request.app["_looper_tasks"] = request.app.get("_looper_tasks", [])
            request.app["_looper_tasks"].append(task)
            return web.json_response({"status": "started", "goal": goal})
        except Exception:  # noqa: BLE001 - never leak internals
            logger.exception("HTTP /build error")
            return web.json_response({"error": "internal error"}, status=500)

    async def stop(self):
        if self._server is not None:
            await self._server.cleanup()


# ============================================================================
# PHASE MANAGER
# ============================================================================


class PhaseManager:
    MAX_RETRIES = 3
    RETRY_BACKOFF = 2.0

    def __init__(self, state: StateManager, workspace: Path):
        if openai is None:
            raise RuntimeError(
                "The 'openai' package is required to call OpenRouter. "
                "Install it with: pip install openai"
            )
        self.state = state
        self.workspace = workspace
        self.prompts = PromptGenerator()
        self.workspace.mkdir(parents=True, exist_ok=True)

        headers = {}
        if OPENROUTER_SITE_URL:
            headers["HTTP-Referer"] = OPENROUTER_SITE_URL
        if OPENROUTER_SITE_NAME:
            headers["X-Title"] = OPENROUTER_SITE_NAME

        self.client = openai.AsyncOpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=os.environ.get(OPENROUTER_API_KEY_ENV, ""),
            default_headers=headers or None,
        )

    def _write_file(self, relative_path: str, content: str):
        path = self.workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        files = self.state.state.get("files_created", [])
        files.append(str(path))
        self.state.update(files_created=files)

    def _read_file(self, relative_path: str) -> str:
        path = self.workspace / relative_path
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    async def _call_agent(self, agent_key: str, prompt: str, extra_system: str = "") -> str:
        """Single entry point for every LLM call.

        Routes to OpenRouter using the model assigned to ``agent_key``.
        Retries transient failures with exponential backoff. On final
        failure returns an ``[ERROR ...]`` string so callers can detect and
        refuse to silently pass (prevents false-positive scores).
        """
        agent = AGENTS[agent_key]
        system_prompt = (
            f"You are the {agent['role']} on an autonomous multi-agent software "
            "engineering team. Stay strictly within this role's responsibilities."
        )
        if extra_system:
            system_prompt += " " + extra_system

        last_error: Exception | None = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = await self.client.chat.completions.create(
                    model=agent["model"],
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=int(agent.get("max_tokens", 8192)),
                    temperature=float(agent.get("temperature", 0.3)),
                )
                return response.choices[0].message.content or ""
            except Exception as exc:  # noqa: BLE001 - retry then surface
                last_error = exc
                logger.warning(
                    "Agent %s attempt %d/%d failed: %s",
                    agent["role"],
                    attempt,
                    self.MAX_RETRIES,
                    exc,
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_BACKOFF**attempt)
        return f"[ERROR calling {agent['role']} ({agent['model']}): {last_error}]"

    # -- Phases --------------------------------------------------------

    async def run_research(self, goal: str) -> dict:
        self.state.update(current_phase="research", status="in_progress")
        result = await self._call_agent("researcher", self.prompts.research(goal))
        self._write_file("research.md", result)
        self.state.update(status="done")
        self.state.save()
        return {
            "phase": "research",
            "status": "done",
            "agent": AGENTS["researcher"]["role"],
            "model": AGENTS["researcher"]["model"],
            "summary": "Research completed",
            "files_created": ["research.md"],
        }

    async def run_architecture(self, goal: str) -> dict:
        self.state.update(current_phase="architecture", status="in_progress")
        research = self._read_file("research.md")
        design = await self._call_agent("architect", self.prompts.architecture(goal, research))
        api_notes = await self._call_agent("ux_api_designer", self.prompts.api_design(goal, design))
        combined = design + "\n\n## API & DX Design (UX/API Designer)\n\n" + api_notes
        self._write_file("architecture/design.md", combined)
        self.state.update(status="done")
        self.state.save()
        return {
            "phase": "architecture",
            "status": "done",
            "agent": f"{AGENTS['architect']['role']} + {AGENTS['ux_api_designer']['role']}",
            "summary": "Architecture + API/UX design completed",
            "files_created": ["architecture/design.md"],
        }

    async def run_build(self, goal: str) -> dict:
        self.state.update(current_phase="build", status="in_progress")
        architecture = self._read_file("architecture/design.md")
        result = await self._call_agent("builder", self.prompts.build(goal, architecture))
        build_ok = not result.startswith("[ERROR")
        self._write_file("src/generated_code.py", result)
        self.state.update(status="done")
        self.state.save()
        return {
            "phase": "build",
            "status": "done" if build_ok else "error",
            "agent": AGENTS["builder"]["role"],
            "model": AGENTS["builder"]["model"],
            "summary": "Code generated" if build_ok else "Code generation failed",
            "files_created": ["src/generated_code.py"],
            "build_ok": build_ok,
        }

    async def run_test(self, goal: str) -> dict:
        self.state.update(current_phase="test", status="in_progress")
        code = self._read_file("src/generated_code.py")
        result = await self._call_agent("tester", self.prompts.test(goal, code))
        self._write_file("tests/test_generated.py", result)

        passed, failed = 0, 0
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "pytest", str(self.workspace / "tests"), "-q"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace),
            )
            passed, failed = parse_test_summary(proc.stdout, proc.stderr)
            if failed == 0 and proc.returncode != 0 and passed == 0:
                failed = max(failed, 1)
        except Exception as exc:  # noqa: BLE001
            failed = max(failed, 1)
            result += f"\n[Test error: {exc}]"
            logger.exception("Test run failed")

        self.state.update(status="done")
        self.state.save()
        return {
            "phase": "test",
            "status": "done",
            "agent": AGENTS["tester"]["role"],
            "model": AGENTS["tester"]["model"],
            "summary": f"Tests: {passed} passed, {failed} failed",
            "files_created": ["tests/test_generated.py"],
            "tests_passed": passed,
            "tests_total": passed + failed,
        }

    async def run_review(self, goal: str) -> dict:
        self.state.update(current_phase="review", status="in_progress")
        code = self._read_file("src/generated_code.py")
        extra_system = (
            "You are a separate reviewer instance from whoever built this code, "
            "with no stake in it being accepted. Be skeptical and thorough, not "
            "agreeable."
        )
        result = await self._call_agent(
            "reviewer", self.prompts.review(goal, code), extra_system=extra_system
        )
        self._write_file("review.md", result)

        review_score = 0.0  # default 0 when agent failed -> never silently passes
        if result.startswith("[ERROR"):
            logger.error("Review agent failed; scoring 0 to avoid false pass")
        else:
            m = re.search(r"score[:\s]+(\d+(?:\.\d+)?)", result, re.IGNORECASE)
            if m:
                review_score = float(m.group(1))

        self.state.update(status="done")
        self.state.save()
        return {
            "phase": "review",
            "status": "done",
            "agent": AGENTS["reviewer"]["role"],
            "model": AGENTS["reviewer"]["model"],
            "summary": f"Review score: {review_score}",
            "files_created": ["review.md"],
            "review_score": review_score,
        }

    async def run_security_audit(self, goal: str) -> dict:
        self.state.update(current_phase="security_audit", status="in_progress")
        code = self._read_file("src/generated_code.py")
        result = await self._call_agent("security_auditor", self.prompts.security_audit(goal, code))
        self._write_file("security_audit.md", result)

        if result.startswith("[ERROR"):
            # Agent failure must NOT be counted as "no issues found".
            logger.error("Security audit failed; recording as a blocking issue")
            security_issues = ["CRITICAL: security audit did not complete"]
        else:
            findings = re.findall(
                r"^[-*]\s*\**\s*(CRITICAL|HIGH|MEDIUM|LOW)\**\s*:?\\s*(.+)$",
                result,
                re.IGNORECASE | re.MULTILINE,
            )
            security_issues = [f"{sev.upper()}: {desc.strip()}" for sev, desc in findings]
            if not security_issues and "no issues found" not in result.lower():
                # Unrecognised formatting - treat conservatively as a flag.
                security_issues = ["MEDIUM: audit output not in expected format"]

        self.state.update(status="done")
        self.state.save()
        return {
            "phase": "security_audit",
            "status": "done",
            "agent": AGENTS["security_auditor"]["role"],
            "model": AGENTS["security_auditor"]["model"],
            "summary": f"{len(security_issues)} security issue(s) found",
            "files_created": ["security_audit.md"],
            "security_issues": security_issues,
        }

    async def run_performance_optimize(self, goal: str) -> dict:
        self.state.update(current_phase="performance_optimize", status="in_progress")
        code = self._read_file("src/generated_code.py")
        result = await self._call_agent(
            "performance_optimizer", self.prompts.performance_optimize(goal, code)
        )
        # Written separately rather than overwriting generated_code.py --
        # this rewrite hasn't been through test/review/security again.
        self._write_file("src/optimized_code.py", result)
        self.state.update(status="done")
        self.state.save()
        return {
            "phase": "performance_optimize",
            "status": "done",
            "agent": AGENTS["performance_optimizer"]["role"],
            "model": AGENTS["performance_optimizer"]["model"],
            "summary": "Performance pass completed",
            "files_created": ["src/optimized_code.py"],
        }

    async def run_documentation(self, goal: str) -> dict:
        self.state.update(current_phase="documentation", status="in_progress")
        architecture = self._read_file("architecture/design.md")
        code = self._read_file("src/optimized_code.py") or self._read_file("src/generated_code.py")
        result = await self._call_agent(
            "documentation_writer", self.prompts.documentation(goal, architecture, code)
        )
        self._write_file("docs/README.md", result)
        self.state.update(status="done")
        self.state.save()
        return {
            "phase": "documentation",
            "status": "done",
            "agent": AGENTS["documentation_writer"]["role"],
            "model": AGENTS["documentation_writer"]["model"],
            "summary": "Documentation generated",
            "files_created": ["docs/README.md"],
        }

    async def run_fix(self, goal: str, issues: list) -> dict:
        self.state.update(current_phase="fix", status="in_progress")
        code = self._read_file("src/generated_code.py")
        result = await self._call_agent("fixer", self.prompts.fix(goal, code, issues))
        build_ok = not result.startswith("[ERROR")

        cycle = self.state.state.get("cycle", 0)
        archive_path = f"src/fixes_cycle_{cycle}.py"
        self._write_file(archive_path, result)
        files_created = [archive_path]
        if build_ok:
            # Patch becomes the canonical code that later phases read.
            self._write_file("src/generated_code.py", result)
            files_created.append("src/generated_code.py")

        self.state.update(status="done")
        self.state.save()
        return {
            "phase": "fix",
            "status": "done" if build_ok else "error",
            "agent": AGENTS["fixer"]["role"],
            "model": AGENTS["fixer"]["model"],
            "summary": "Fixes applied" if build_ok else "Fix generation failed",
            "files_created": files_created,
            "build_ok": build_ok,
        }


# ============================================================================
# LOOPER DAEMON
# ============================================================================


class LooperDaemon:
    def __init__(self):
        self.state = StateManager(STATE_FILE)
        self.phases = PhaseManager(self.state, WORKSPACE)
        self.scoring = ScoringEngine()
        self.watcher = FileWatcher(WATCH_FILE, self._on_command)
        self.http = HTTPServer(HTTP_PORT, HTTP_BIND, self._on_goal, HTTP_AUTH_TOKEN)
        self._running = False

    async def start(self):
        logger.info("Starting daemon on %s:%s...", HTTP_BIND, HTTP_PORT)
        logger.info("Watching %s...", WATCH_FILE)
        self._running = True
        try:
            await asyncio.gather(self.watcher.start(), self.http.start())
        finally:
            await self.http.stop()

    async def _on_command(self, content: str):
        if content:
            logger.info("Command received: %s...", content[:100])
            await self.build(content)

    async def _on_goal(self, goal: str):
        logger.info("HTTP goal received: %s...", goal[:100])
        await self.build(goal)

    async def build(self, goal: str):
        """Orchestrator (deterministic, no LLM call) -- routes work to the
        agents below and decides when to retry, fix, or finish.
        """
        self.state.reset()
        self.state.update(current_goal=goal, status="running")
        self.state.save()

        cycle = 0
        build_ok = True
        final_score = 0.0
        fix_attempts = 0

        while cycle < MAX_CYCLES:
            cycle += 1
            self.state.update(cycle=cycle)
            self.state.save()
            logger.info("=== CYCLE %d ===", cycle)

            phases_to_run = FIRST_CYCLE_PHASES if cycle == 1 else RETRY_CYCLE_PHASES
            review_score = 0.0
            security_issues = []
            tests_passed, tests_total = 0, 0

            for phase_name in phases_to_run:
                logger.info("Phase: %s", phase_name)
                handler = getattr(self.phases, f"run_{phase_name}", None)
                if not handler:
                    continue
                result = await handler(goal)
                self._log(result)

                if phase_name == "build":
                    build_ok = result.get("build_ok", True)
                elif phase_name == "test":
                    tests_passed = result.get("tests_passed", 0)
                    tests_total = result.get("tests_total", 0)
                elif phase_name == "review":
                    review_score = result.get("review_score", 0.0)
                elif phase_name == "security_audit":
                    security_issues = result.get("security_issues", [])

            score = self.scoring.calculate_score(
                build_ok, tests_passed, tests_total, security_issues, review_score
            )
            final_score = score
            self.state.update(score=score)
            self.state.save()
            logger.info("Score: %.2f", score)

            if score >= TARGET_SCORE:
                break

            if score < MIN_ACCEPTABLE and cycle < MAX_CYCLES:
                if fix_attempts >= MAX_CYCLES - cycle:
                    logger.warning("Fix budget exhausted; stopping retries")
                    break
                issues = [f"Review score was {review_score}/100 (target {TARGET_SCORE})"]
                issues += security_issues
                fix_result = await self.phases.run_fix(goal, issues)
                self._log(fix_result)
                build_ok = fix_result.get("build_ok", build_ok)
                fix_attempts += 1

        if final_score >= MIN_ACCEPTABLE:
            for phase_name in FINAL_PHASES:
                logger.info("Phase: %s", phase_name)
                handler = getattr(self.phases, f"run_{phase_name}", None)
                if not handler:
                    continue
                result = await handler(goal)
                self._log(result)
        else:
            logger.warning(
                "Final score %.2f below minimum %.2f; skipping performance/documentation polish.",
                final_score,
                MIN_ACCEPTABLE,
            )

        self.state.update(status="done", current_phase="done")
        self.state.save()
        logger.info("Build complete. Final score: %.2f", final_score)

    def _log(self, result: dict):
        history = self.state.state.get("history", [])
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cycle": self.state.state.get("cycle", 0),
            **result,
        }
        history.append(entry)
        self.state.update(history=history)
        self.state.save()
        logger.info(json.dumps(entry, indent=2, ensure_ascii=False))


# ============================================================================
# MAIN
# ============================================================================


def main():
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Looper Autonomous Daemon")
    parser.add_argument("--goal", type=str, help="Build goal directly")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon")
    parser.add_argument("--reset", action="store_true", help="Reset state")
    parser.add_argument("--config", type=str, default=None, help="Path to config YAML")
    args = parser.parse_args()

    if args.config:
        configure(load_config(args.config))
    else:
        configure()

    daemon = LooperDaemon()

    if args.reset:
        daemon.state.reset()
        logger.info("State reset.")
        return

    if args.goal:
        asyncio.run(daemon.build(args.goal))
        return

    if args.daemon:
        asyncio.run(daemon.start())
        return

    parser.print_help()


if __name__ == "__main__":
    main()
