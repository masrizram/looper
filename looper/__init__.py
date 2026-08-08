"""Looper - Autonomous AI Software Engineering System.

Importing this package has NO side effects: no config file is read and no
network client is built until you explicitly call :func:`looper.config.load_config`
and construct :class:`looper.orchestrator.LooperDaemon`.
"""

from looper.config import AgentSpec, LooperConfig, load_config
from looper.orchestrator import LooperDaemon
from looper.phases import PhaseManager, PhaseResult
from looper.scoring import ScoringEngine
from looper.state import StateManager

__version__ = "2.1.0"

__all__ = [
    "AgentSpec",
    "LooperConfig",
    "LooperDaemon",
    "PhaseManager",
    "PhaseResult",
    "ScoringEngine",
    "StateManager",
    "load_config",
    "__version__",
]
