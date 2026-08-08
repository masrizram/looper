#!/usr/bin/env python3
"""Backwards-compatible shim for the pre-2.0 single-file entry point.

The implementation now lives in the ``looper`` package (see ADR-001). This
module re-exports the public names and delegates ``main()`` so existing
scripts, systemd units, and docs that call ``python daemon.py --daemon``
keep working.

Unlike the old module, importing this has **no side effects**: no config file
is read and no network client is constructed at import time.
"""

from __future__ import annotations

from looper import __version__
from looper.cli import main
from looper.config import (
    DEFAULT_AGENTS,
    AgentSpec,
    ConfigError,
    LooperConfig,
    build_config,
    load_config,
)
from looper.llm import AgentReply, OpenRouterClient
from looper.orchestrator import LooperDaemon
from looper.phases import PhaseManager, PhaseResult
from looper.prompts import PromptGenerator
from looper.scoring import SECURITY_FINDING_RE, ScoringEngine, parse_security_findings
from looper.server import HTTPServer, RateLimiter
from looper.state import StateManager
from looper.testparse import parse_test_summary
from looper.watcher import FileWatcher

__all__ = [
    "AgentReply",
    "AgentSpec",
    "ConfigError",
    "DEFAULT_AGENTS",
    "FileWatcher",
    "HTTPServer",
    "LooperConfig",
    "LooperDaemon",
    "OpenRouterClient",
    "PhaseManager",
    "PhaseResult",
    "PromptGenerator",
    "RateLimiter",
    "SECURITY_FINDING_RE",
    "ScoringEngine",
    "StateManager",
    "__version__",
    "build_config",
    "load_config",
    "main",
    "parse_security_findings",
    "parse_test_summary",
]


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
