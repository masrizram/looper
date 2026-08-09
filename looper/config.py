"""Immutable, validated configuration objects.

Design decisions (ADR-001):

* Config is a frozen dataclass tree, not a dict of module-level globals.
  The previous design called ``configure()`` at import time, which made
  ``import daemon`` fail outright when no ``config.yaml`` sat in the CWD and
  made two differently-configured instances impossible.
* Every value is validated once, at construction, so the rest of the codebase
  can treat the config as trusted and skip defensive ``.get(..., default)``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from looper.sandbox import SANDBOX_BACKENDS

logger = logging.getLogger("looper.config")

DEFAULT_CONFIG_FILENAMES = ("config.yaml", "looper_config.yaml")

DEFAULT_FIRST_CYCLE_PHASES = (
    "research",
    "architecture",
    "build",
    "test",
    "review",
    "security_audit",
)
DEFAULT_RETRY_CYCLE_PHASES = ("test", "review", "security_audit")
DEFAULT_FINAL_PHASES = ("performance_optimize", "documentation")

KNOWN_PHASES = frozenset(
    {
        "research",
        "architecture",
        "build",
        "test",
        "review",
        "security_audit",
        "performance_optimize",
        "documentation",
    }
)

LOOPBACK_BINDS = frozenset({"127.0.0.1", "localhost", "::1"})
ALL_INTERFACES = "0.0.0.0"  # nosec B104 - compared against, never bound by default


class ConfigError(ValueError):
    """Raised when the supplied configuration is invalid."""


class CostBudgetExceeded(RuntimeError):
    """Raised when a build's estimated API spend crosses ``max_cost_usd``.

    Carries the spend so the CLI can report it and exit with code 4. ADR-005.
    """

    def __init__(self, spent_usd: float, limit_usd: float) -> None:
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        super().__init__(f"cost budget {limit_usd:.2f} USD exhausted (spent {spent_usd:.2f})")


def _require_int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an int, got {value!r}")
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _require_number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    if not low <= float(value) <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return float(value)


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One LLM role: which model answers, and with what sampling settings."""

    model: str
    role: str
    temperature: float = 0.3
    max_tokens: int = 8192

    def __post_init__(self) -> None:
        if not self.model or not isinstance(self.model, str):
            raise ConfigError(f"agent model must be a non-empty string, got {self.model!r}")
        if not self.role or not isinstance(self.role, str):
            raise ConfigError(f"agent role must be a non-empty string, got {self.role!r}")
        _require_number(self.temperature, f"agent[{self.role}].temperature", 0.0, 2.0)
        _require_int(self.max_tokens, f"agent[{self.role}].max_tokens", 1, 1_000_000)


#: Default agent roster. Model slugs were verified against the live OpenRouter
#: catalogue (``GET /api/v1/models``); ``looper --check-models`` re-verifies
#: them, because a slug that 404s only fails at build time otherwise.
#:
#: Tiering follows cost/capability rather than brand:
#:   reasoning-heavy, low token volume  -> Opus 5      (~$5/$25 per M)
#:   adversarial review, needs a *different* family than the builder
#:                                      -> GPT-5.6 Sol (~$5/$30 per M)
#:   code generation, high token volume -> Sonnet 5    (~$2/$10 per M)
#:   long-context prose, cheap          -> Gemini 3.1 Pro / Terra
DEFAULT_AGENTS: Mapping[str, AgentSpec] = {
    "researcher": AgentSpec("anthropic/claude-opus-5", "Senior Technical Researcher", 0.3),
    "architect": AgentSpec("anthropic/claude-opus-5", "System Architect", 0.3),
    "ux_api_designer": AgentSpec("openai/gpt-5.6-sol", "UX/API Designer", 0.4),
    "builder": AgentSpec("anthropic/claude-sonnet-5", "Code Builder", 0.2),
    # Test design is where overfitting is caught, so it gets a frontier model
    # from a different family than the builder (ADR-006).
    "tester": AgentSpec("anthropic/claude-opus-5", "Test Generator", 0.3),
    "reviewer": AgentSpec("openai/gpt-5.6-terra", "Senior Reviewer", 0.2),
    "security_auditor": AgentSpec("openai/gpt-5.6-sol", "Security Auditor", 0.2),
    "performance_optimizer": AgentSpec("anthropic/claude-sonnet-5", "Performance Optimizer", 0.2),
    "documentation_writer": AgentSpec("google/gemini-3.1-pro-preview", "Documentation Writer", 0.4),
    "fixer": AgentSpec("anthropic/claude-sonnet-5", "Expert Fixer", 0.2),
}

#: Blended USD per 1K tokens for the default roster, from the live OpenRouter
#: catalogue. Without these the cost budget prices an Opus call at the generic
#: $0.002/1K guess and under-reports spend by roughly 7x, which would make
#: ``max_cost_usd`` (ADR-005) a budget in name only.
DEFAULT_MODEL_PRICES_USD_PER_1K: Mapping[str, float] = {
    "anthropic/claude-opus-5": 0.015,
    "openai/gpt-5.6-sol": 0.0175,
    "anthropic/claude-sonnet-5": 0.006,
    "openai/gpt-5.6-terra": 0.0035,
    "google/gemini-3.1-pro-preview": 0.007,
    "deepseek/deepseek-v4-pro": 0.00065,
    "deepseek/deepseek-v4-flash": 0.00021,
    "x-ai/grok-4.5": 0.004,
}

#: Per-model USD per 1K *completion* tokens. Empty by default on purpose: the
#: prices above are already blended input/output figures, so inventing a
#: multiplier here would make the estimate less accurate, not more. Operators
#: who know their real split can set ``execution.completion_prices_usd_per_1k``
#: and get exact accounting; everyone else keeps the blended behaviour.
DEFAULT_COMPLETION_PRICES_USD_PER_1K: Mapping[str, float] = {}

#: Phase name -> agent key. Used to validate that configured phases are runnable.
PHASE_AGENTS: Mapping[str, str] = {
    "research": "researcher",
    "architecture": "architect",
    "build": "builder",
    "test": "tester",
    "review": "reviewer",
    "security_audit": "security_auditor",
    "performance_optimize": "performance_optimizer",
    "documentation": "documentation_writer",
}


@dataclass(frozen=True, slots=True)
class HTTPConfig:
    bind: str = "127.0.0.1"
    port: int = 9999
    auth_token_env: str = "LOOPER_HTTP_TOKEN"
    auth_token: str = ""
    max_goal_length: int = 20_000
    rate_limit_per_minute: int = 10
    max_body_bytes: int = 65_536
    #: Peer addresses whose ``X-Forwarded-For`` header may be believed. Behind
    #: a reverse proxy every request otherwise shares one remote address, so
    #: the per-client rate limit degrades into a single global bucket. Empty
    #: (the default) means the header is ignored entirely - trusting it
    #: unconditionally would let any caller forge an identity and bypass the
    #: limit outright.
    trusted_proxies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_int(self.port, "http.port", 1, 65535)
        _require_int(self.max_goal_length, "http.max_goal_length", 1, 1_000_000)
        _require_int(self.rate_limit_per_minute, "http.rate_limit_per_minute", 1, 100_000)
        _require_int(self.max_body_bytes, "http.max_body_bytes", 1, 100_000_000)
        if not isinstance(self.bind, str) or not self.bind:
            raise ConfigError(f"http.bind must be a non-empty string, got {self.bind!r}")
        if not isinstance(self.trusted_proxies, tuple):
            raise ConfigError(f"http.trusted_proxies must be a list, got {self.trusted_proxies!r}")

        # ANY non-loopback bind is network-reachable, not just 0.0.0.0. The
        # earlier check special-cased 0.0.0.0 only, so binding a LAN address
        # (or "::", the IPv6 wildcard) exposed the RCE-capable /build endpoint
        # with no token at all.
        if self.bind not in LOOPBACK_BINDS:
            if not self.auth_token:
                raise ConfigError(
                    f"Refusing to bind non-loopback address {self.bind!r} without an "
                    f"auth token. Set ${self.auth_token_env}, or bind 127.0.0.1. The "
                    "/build endpoint triggers arbitrary LLM-driven code execution."
                )
            logger.warning(
                "Binding %s: the API is reachable from the network. "
                "Front it with a reverse proxy and TLS.",
                self.bind,
            )

    @property
    def is_public(self) -> bool:
        return self.bind not in LOOPBACK_BINDS


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """Score composition. All four buckets must sum to 100."""

    build: float = 20.0
    tests: float = 30.0
    security: float = 30.0
    review: float = 20.0

    critical: float = 30.0
    high: float = 15.0
    medium: float = 5.0
    low: float = 2.0
    unknown: float = 5.0

    unverified_build_cap: float = 60.0
    critical_finding_cap: float = 50.0
    #: A report carrying at least this many findings is capped like a critical
    #: one, whatever the individual severities say. Severity penalties alone
    #: saturate: 50 UNKNOWN findings zero the security bucket and then stop
    #: mattering, so sheer volume could clear the gate on the other three.
    findings_volume_threshold: int = 15

    def __post_init__(self) -> None:
        for name in ("build", "tests", "security", "review"):
            _require_number(getattr(self, name), f"scoring.{name}", 0.0, 100.0)
        total = self.build + self.tests + self.security + self.review
        if abs(total - 100.0) > 1e-6:
            raise ConfigError(f"scoring weights must sum to 100, got {total}")
        for name in ("critical", "high", "medium", "low", "unknown"):
            _require_number(getattr(self, name), f"scoring.severity.{name}", 0.0, 100.0)
        for name in ("unverified_build_cap", "critical_finding_cap"):
            _require_number(getattr(self, name), f"scoring.{name}", 0.0, 100.0)
        _require_int(
            self.findings_volume_threshold,
            "scoring.findings_volume_threshold",
            1,
            10_000,
        )

    def penalty_for(self, severity: str) -> float:
        return {
            "CRITICAL": self.critical,
            "HIGH": self.high,
            "MEDIUM": self.medium,
            "LOW": self.low,
        }.get(severity.upper(), self.unknown)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_base: float = 2.0
    backoff_max: float = 60.0

    def __post_init__(self) -> None:
        _require_int(self.max_attempts, "retry.max_attempts", 1, 100)
        _require_number(self.backoff_base, "retry.backoff_base", 1.0, 100.0)
        _require_number(self.backoff_max, "retry.backoff_max", 0.0, 3600.0)

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before ``attempt`` + 1. Capped at ``backoff_max``."""
        return min(self.backoff_max, self.backoff_base**attempt)


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    max_cycles: int = 5
    target_score: float = 99.0
    min_acceptable: float = 95.0
    test_timeout_seconds: int = 600
    max_history_entries: int = 500
    #: Ceiling on any single file written from agent output. An LLM that
    #: loops can otherwise fill the disk of an unattended 24/7 daemon.
    max_file_bytes: int = 2_000_000
    #: Hard cost ceiling in USD for a single `build`. When the running token
    #: estimate crosses it the build is aborted hard (exit code 4) instead of
    #: silently running up the bill. 0 disables the cap. ADR-005.
    max_cost_usd: float = 0.0
    #: Per-model USD price per 1K tokens, used to estimate spend from usage.
    #: Defaults to the verified prices for the default roster; anything the
    #: user supplies is merged over the top. Missing models fall back to
    #: ``default_token_price_usd``. 0 == unknown.
    model_prices_usd_per_1k: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_MODEL_PRICES_USD_PER_1K)
    )
    default_token_price_usd: float = 0.002
    #: Per-model USD price per 1K *completion* tokens. Providers bill output
    #: at 3-5x the input rate, so pricing both sides the same under-reported
    #: every output-heavy run. Missing entries fall back to the prompt price,
    #: which is exactly the previous behaviour.
    completion_prices_usd_per_1k: Mapping[str, float] = field(default_factory=dict)
    #: Optional path (relative to --config dir or absolute) to a user-owned
    #: pytest suite. If set, the generated code must ALSO pass the user's
    #: tests before the build can clear the gate - closes the self-test
    #: overfitting hole (an AI writing trivially-passing tests for itself).
    user_tests_dir: str = ""
    #: Reject generated test suites that look like they were written to pass
    #: rather than to verify: require at least this many `assert`/raises per
    #: hundred lines of generated test code, and forbid hardcoding the
    #: expected score. 0 disables the floor.
    min_test_assertions_per_100_lines: int = 6
    #: Run the generated pytest suite under OS resource limits (CPU seconds,
    #: wall clock, RSS) so a `while True`, fork bomb, or memory hog in
    #: LLM-authored code cannot wedge or OOM the host. Always static-scan the
    #: suite for destructive/network calls first and refuse to run it.
    sandbox_tests: bool = True
    sandbox_cpu_seconds: int = 60
    #: Docker/Podman ``--cpus`` scheduler share. Separate from
    #: ``sandbox_cpu_seconds`` on purpose: one is a throttle, the other a hard
    #: kill. Deriving the share from the budget gave a long-running sandbox
    #: *more* CPU, which is backwards.
    sandbox_cpu_shares: float = 1.0
    sandbox_wall_seconds: int = 300
    sandbox_rss_bytes: int = 1_000_000_000
    #: Which isolation backend to use: auto|rlimit|docker|podman|none.
    #: "auto" prefers Docker (identical guarantees on every OS) then Podman
    #: then POSIX rlimits. ADR-008.
    sandbox_backend: str = "auto"
    sandbox_image: str = "python:3.11-slim"
    #: Docker network mode for the sandbox container. "none" = no network.
    sandbox_network: str = "none"
    #: When no isolation backend is available, REFUSE to run LLM-authored
    #: tests rather than silently running them unconfined. Setting this false
    #: reintroduces the fail-open hole and is logged loudly. ADR-008.
    sandbox_fail_closed: bool = True
    #: single_file -> one src/generated_code.py; package -> the builder may
    #: emit a multi-file tree, capped by max_files_per_build. ADR-009.
    artifact_mode: str = "single_file"
    max_files_per_build: int = 25
    #: Commit each build's workspace to git on a dedicated branch so the
    #: output is reviewable with the normal `git diff` tooling. ADR-010.
    git_enabled: bool = False
    git_branch_prefix: str = "looper/"
    git_commit_per_cycle: bool = True
    git_author_name: str = "looper"
    git_author_email: str = "looper@localhost"
    #: Lint the generated code with `python -m py_compile`/flake8 before it is
    #: accepted, so obviously-broken or style-corrupt output never reaches the
    #: "done" state. Set to "off" to skip.
    lint_generated: str = "py_compile"

    def __post_init__(self) -> None:
        _require_int(self.max_cycles, "execution.max_cycles", 1, 1000)
        _require_number(self.target_score, "execution.target_score", 0.0, 100.0)
        _require_number(self.min_acceptable, "execution.min_acceptable", 0.0, 100.0)
        _require_int(self.test_timeout_seconds, "execution.test_timeout_seconds", 1, 86_400)
        _require_int(self.max_history_entries, "execution.max_history_entries", 1, 1_000_000)
        _require_int(self.max_file_bytes, "execution.max_file_bytes", 1024, 1_000_000_000)
        _require_number(self.max_cost_usd, "execution.max_cost_usd", 0.0, 1_000_000.0)
        _require_number(
            self.default_token_price_usd,
            "execution.default_token_price_usd",
            0.0,
            100.0,
        )
        _require_int(
            self.min_test_assertions_per_100_lines,
            "execution.min_test_assertions_per_100_lines",
            0,
            1000,
        )
        _require_int(self.sandbox_cpu_seconds, "execution.sandbox_cpu_seconds", 1, 86_400)
        _require_number(self.sandbox_cpu_shares, "execution.sandbox_cpu_shares", 0.1, 256.0)
        _require_int(self.sandbox_wall_seconds, "execution.sandbox_wall_seconds", 1, 86_400)
        _require_int(
            self.sandbox_rss_bytes,
            "execution.sandbox_rss_bytes",
            1_000_000,
            1_000_000_000_000,
        )
        if self.min_acceptable > self.target_score:
            raise ConfigError(
                "execution.min_acceptable must be <= target_score, got "
                f"min={self.min_acceptable}, target={self.target_score}"
            )
        if self.lint_generated not in ("off", "py_compile", "flake8"):
            raise ConfigError(
                "execution.lint_generated must be off|py_compile|flake8, got "
                f"{self.lint_generated!r}"
            )
        if self.sandbox_backend not in SANDBOX_BACKENDS:
            raise ConfigError(
                "execution.sandbox_backend must be one of "
                f"{list(SANDBOX_BACKENDS)}, got {self.sandbox_backend!r}"
            )
        if not self.sandbox_image:
            raise ConfigError("execution.sandbox_image must not be empty")
        if self.artifact_mode not in ("single_file", "package"):
            raise ConfigError(
                "execution.artifact_mode must be single_file|package, got "
                f"{self.artifact_mode!r}"
            )
        _require_int(self.max_files_per_build, "execution.max_files_per_build", 1, 1000)
        if not self.git_branch_prefix:
            raise ConfigError("execution.git_branch_prefix must not be empty")


@dataclass(frozen=True, slots=True)
class OpenRouterConfig:
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    api_key: str = ""
    site_url: str = ""
    site_name: str = "Looper Daemon"
    #: Hard ceiling on a single LLM call. Without it one stalled connection
    #: wedges a phase, and therefore the whole 24/7 daemon, indefinitely.
    request_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"openrouter.base_url must be http(s), got {self.base_url!r}")
        _require_number(
            self.request_timeout_seconds,
            "openrouter.request_timeout_seconds",
            1.0,
            3600.0,
        )

    def default_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.site_name:
            headers["X-Title"] = self.site_name
        return headers


def _validate_phases(names: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(names, str) or not isinstance(names, (list, tuple)):
        raise ConfigError(f"{field_name} must be a list of phase names, got {names!r}")
    result = tuple(str(n) for n in names)
    unknown = [n for n in result if n not in KNOWN_PHASES]
    if unknown:
        raise ConfigError(
            f"{field_name} contains unknown phase(s) {unknown}; "
            f"known phases are {sorted(KNOWN_PHASES)}"
        )
    if len(set(result)) != len(result):
        raise ConfigError(f"{field_name} contains duplicate phases: {result}")
    return result


@dataclass(frozen=True, slots=True)
class LooperConfig:
    """Fully validated runtime configuration."""

    workspace: Path = Path("./workspace")
    state_file: Path = Path("./looper_state.json")
    watch_file: Path = Path("./looper_commands.txt")
    watch_interval: float = 2.0

    http: HTTPConfig = field(default_factory=HTTPConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    scoring: ScoringWeights = field(default_factory=ScoringWeights)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)

    agents: Mapping[str, AgentSpec] = field(default_factory=lambda: dict(DEFAULT_AGENTS))

    first_cycle_phases: tuple[str, ...] = DEFAULT_FIRST_CYCLE_PHASES
    retry_cycle_phases: tuple[str, ...] = DEFAULT_RETRY_CYCLE_PHASES
    final_phases: tuple[str, ...] = DEFAULT_FINAL_PHASES

    def __post_init__(self) -> None:
        _require_number(self.watch_interval, "watch_interval", 0.01, 3600.0)
        missing = [k for k in DEFAULT_AGENTS if k not in self.agents]
        if missing:
            raise ConfigError(f"missing agent definitions: {sorted(missing)}")

    def with_(self, **changes: Any) -> LooperConfig:
        """Return a copy with ``changes`` applied (config stays immutable)."""
        return replace(self, **changes)


def _read_env(name: str, env: Mapping[str, str] | None) -> str:
    source = os.environ if env is None else env
    return source.get(name, "") or ""


def build_config(
    raw: Mapping[str, Any] | None, env: Mapping[str, str] | None = None
) -> LooperConfig:
    """Turn a raw mapping (typically parsed YAML) into a validated config.

    ``env`` is injectable so tests never have to mutate the real environment.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")

    def section(name: str) -> Mapping[str, Any]:
        value = raw.get(name) or {}
        if not isinstance(value, Mapping):
            raise ConfigError(f"config section {name!r} must be a mapping, got {value!r}")
        return value

    http_raw = section("http")
    auth_env = str(http_raw.get("auth_token_env", "LOOPER_HTTP_TOKEN"))
    # Legacy top-level `http_port` is still honoured so old configs keep working.
    port = http_raw.get("port", raw.get("http_port", 9999))
    http = HTTPConfig(
        bind=str(http_raw.get("bind", "127.0.0.1")),
        port=port,
        auth_token_env=auth_env,
        auth_token=_read_env(auth_env, env),
        max_goal_length=http_raw.get("max_goal_length", 20_000),
        rate_limit_per_minute=http_raw.get("rate_limit_per_minute", 10),
        max_body_bytes=http_raw.get("max_body_bytes", 65_536),
        trusted_proxies=tuple(str(p) for p in (http_raw.get("trusted_proxies") or ())),
    )

    exec_raw = section("execution")
    git_raw = exec_raw.get("git") or {}
    if not isinstance(git_raw, Mapping):
        raise ConfigError(f"execution.git must be a mapping, got {git_raw!r}")
    execution = ExecutionConfig(
        max_cycles=exec_raw.get("max_cycles", 5),
        target_score=exec_raw.get("target_score", 99.0),
        min_acceptable=exec_raw.get("min_acceptable", 95.0),
        test_timeout_seconds=exec_raw.get("test_timeout_seconds", 600),
        max_history_entries=exec_raw.get("max_history_entries", 500),
        max_file_bytes=exec_raw.get("max_file_bytes", 2_000_000),
        max_cost_usd=exec_raw.get("max_cost_usd", 0.0),
        model_prices_usd_per_1k={
            **DEFAULT_MODEL_PRICES_USD_PER_1K,
            **exec_raw.get("model_prices_usd_per_1k", {}),
        },
        default_token_price_usd=exec_raw.get("default_token_price_usd", 0.002),
        completion_prices_usd_per_1k={
            **DEFAULT_COMPLETION_PRICES_USD_PER_1K,
            **exec_raw.get("completion_prices_usd_per_1k", {}),
        },
        user_tests_dir=exec_raw.get("user_tests_dir", ""),
        min_test_assertions_per_100_lines=exec_raw.get("min_test_assertions_per_100_lines", 6),
        sandbox_tests=exec_raw.get("sandbox_tests", True),
        sandbox_cpu_seconds=exec_raw.get("sandbox_cpu_seconds", 60),
        sandbox_cpu_shares=exec_raw.get("sandbox_cpu_shares", 1.0),
        sandbox_wall_seconds=exec_raw.get("sandbox_wall_seconds", 300),
        sandbox_rss_bytes=exec_raw.get("sandbox_rss_bytes", 1_000_000_000),
        sandbox_backend=str(exec_raw.get("sandbox_backend", "auto")),
        sandbox_image=str(exec_raw.get("sandbox_image", "python:3.11-slim")),
        sandbox_network=str(exec_raw.get("sandbox_network", "none")),
        sandbox_fail_closed=bool(exec_raw.get("sandbox_fail_closed", True)),
        artifact_mode=str(exec_raw.get("artifact_mode", "single_file")),
        max_files_per_build=exec_raw.get("max_files_per_build", 25),
        git_enabled=bool(git_raw.get("enabled", False)),
        git_branch_prefix=str(git_raw.get("branch_prefix", "looper/")),
        git_commit_per_cycle=bool(git_raw.get("commit_per_cycle", True)),
        git_author_name=str(git_raw.get("author_name", "looper")),
        git_author_email=str(git_raw.get("author_email", "looper@localhost")),
        lint_generated=exec_raw.get("lint_generated", "py_compile"),
    )

    scoring_raw = section("scoring")
    severity_raw = scoring_raw.get("severity") or {}
    if not isinstance(severity_raw, Mapping):
        raise ConfigError(f"scoring.severity must be a mapping, got {severity_raw!r}")
    scoring = ScoringWeights(
        build=scoring_raw.get("build", 20.0),
        tests=scoring_raw.get("tests", 30.0),
        security=scoring_raw.get("security", 30.0),
        review=scoring_raw.get("review", 20.0),
        critical=severity_raw.get("critical", 30.0),
        high=severity_raw.get("high", 15.0),
        medium=severity_raw.get("medium", 5.0),
        low=severity_raw.get("low", 2.0),
        unknown=severity_raw.get("unknown", 5.0),
        unverified_build_cap=scoring_raw.get("unverified_build_cap", 60.0),
        critical_finding_cap=scoring_raw.get("critical_finding_cap", 50.0),
        findings_volume_threshold=scoring_raw.get("findings_volume_threshold", 15),
    )

    retry_raw = section("retry")
    retry = RetryPolicy(
        max_attempts=retry_raw.get("max_attempts", 3),
        backoff_base=retry_raw.get("backoff_base", 2.0),
        backoff_max=retry_raw.get("backoff_max", 60.0),
    )

    or_raw = section("openrouter")
    api_key_env = str(or_raw.get("api_key_env", "OPENROUTER_API_KEY"))
    openrouter = OpenRouterConfig(
        base_url=str(or_raw.get("base_url", "https://openrouter.ai/api/v1")),
        api_key_env=api_key_env,
        api_key=_read_env(api_key_env, env),
        site_url=str(or_raw.get("site_url", "")),
        site_name=str(or_raw.get("site_name", "Looper Daemon")),
        request_timeout_seconds=or_raw.get("request_timeout_seconds", 300.0),
    )

    agents_raw = section("agents")
    agents: dict[str, AgentSpec] = {}
    unknown_agents = set(agents_raw) - set(DEFAULT_AGENTS)
    if unknown_agents:
        raise ConfigError(
            f"unknown agent key(s) {sorted(unknown_agents)}; "
            f"valid keys are {sorted(DEFAULT_AGENTS)}"
        )
    for key, default in DEFAULT_AGENTS.items():
        override = agents_raw.get(key) or {}
        if not isinstance(override, Mapping):
            raise ConfigError(f"agents.{key} must be a mapping, got {override!r}")
        agents[key] = AgentSpec(
            model=str(override.get("model", default.model)),
            role=str(override.get("role", default.role)),
            temperature=override.get("temperature", default.temperature),
            max_tokens=override.get("max_tokens", default.max_tokens),
        )

    return LooperConfig(
        workspace=Path(str(raw.get("workspace", "./workspace"))),
        state_file=Path(str(raw.get("state_file", "./looper_state.json"))),
        watch_file=Path(str(raw.get("watch_file", "./looper_commands.txt"))),
        watch_interval=raw.get("watch_interval", 2.0),
        http=http,
        execution=execution,
        scoring=scoring,
        retry=retry,
        openrouter=openrouter,
        agents=agents,
        first_cycle_phases=_validate_phases(
            raw.get("phases", list(DEFAULT_FIRST_CYCLE_PHASES)), "phases"
        ),
        retry_cycle_phases=_validate_phases(
            raw.get("retry_phases", list(DEFAULT_RETRY_CYCLE_PHASES)), "retry_phases"
        ),
        final_phases=_validate_phases(
            raw.get("final_phases", list(DEFAULT_FINAL_PHASES)), "final_phases"
        ),
    )


def load_config(
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> LooperConfig:
    """Load and validate config from YAML.

    With no ``path``, tries the known default filenames in order so a repo
    carrying either ``config.yaml`` or ``looper_config.yaml`` works.
    """
    config, _ = load_config_with_dir(path, env=env)
    return config


def load_config_with_dir(
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[LooperConfig, Path | None]:
    """Like :func:`load_config`, but also returns the resolved config dir.

    The dir is used to resolve a relative ``execution.user_tests_dir`` so the
    daemon does not depend on the caller's CWD.
    """
    candidates: list[str | os.PathLike[str]]
    candidates = [path] if path is not None else list(DEFAULT_CONFIG_FILENAMES)

    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                parsed = yaml.safe_load(handle)
        except FileNotFoundError:
            continue
        except yaml.YAMLError as exc:
            raise ConfigError(f"{candidate} is not valid YAML: {exc}") from exc
        logger.info("Loading config from %s", candidate)
        resolved = Path(candidate).resolve() if Path(candidate).exists() else None
        return build_config(parsed, env=env), (resolved.parent if resolved else None)

    if path is not None:
        raise FileNotFoundError(f"Config file not found: {path}")
    raise FileNotFoundError(
        "No config file found. Looked for: " + ", ".join(DEFAULT_CONFIG_FILENAMES)
    )
