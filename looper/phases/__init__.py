"""Phase execution, split by responsibility (ADR-009).

``phases.py`` had grown to 750 lines carrying five jobs at once: agent
orchestration, filesystem writes, subprocess execution, linting, and result
types. That is the same SRP violation the rest of the package avoids, and it
made the trust boundary hard to see -- the code that *runs untrusted output*
sat in the same file as the code that *asks an agent a question*.

The split is by responsibility, not by size:

* :mod:`looper.phases.results`    -- data contract (PhaseResult, CycleEvidence)
* :mod:`looper.phases.workspace`  -- the only filesystem sink, path containment
* :mod:`looper.phases.execution`  -- everything that runs untrusted code
* :mod:`looper.phases.agents`     -- the nine pipeline stages

:class:`PhaseManager` composes the three mixins, so every public name that
lived in the old module -- ``PhaseManager``, ``PhaseResult``, ``CycleEvidence``,
``CODE_FILE``, ``strip_code_fences``, ``run_sandboxed`` -- is still importable
from ``looper.phases``. This is a pure refactor: no behaviour changes.
"""

from __future__ import annotations

from pathlib import Path

from looper.config import LooperConfig
from looper.llm import AgentReply, OpenRouterClient
from looper.phases.agents import REVIEW_SCORE_RE, AgentPhasesMixin
from looper.phases.execution import ExecutionMixin
from looper.phases.results import (
    CycleEvidence,
    PhaseResult,
    WorkspaceEscapeError,
    replace_result,
)
from looper.phases.workspace import (
    CODE_FILE,
    DESIGN_FILE,
    DOCS_FILE,
    OPTIMIZED_FILE,
    RESEARCH_FILE,
    REVIEW_FILE,
    SECURITY_FILE,
    TESTS_FILE,
    WorkspaceMixin,
    strip_code_fences,
)
from looper.prompts import PromptGenerator

# Re-exported so ``monkeypatch.setattr("looper.phases.run_sandboxed", ...)``
# and the equivalent patches in existing tests keep resolving here.
from looper.sandbox import (  # noqa: F401  (re-export)
    SandboxUnavailableError,
    run_sandboxed,
    scan_for_dangerous_calls,
)
from looper.state import StateManager


class PhaseManager(WorkspaceMixin, ExecutionMixin, AgentPhasesMixin):
    """Runs individual pipeline phases against a workspace.

    MRO order matters: :class:`WorkspaceMixin` provides the real
    ``resolve_in_workspace``/``write_file`` that the other two declare as
    contracts, so it must come first.
    """

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


__all__ = [
    "AgentReply",
    "CODE_FILE",
    "CycleEvidence",
    "DESIGN_FILE",
    "DOCS_FILE",
    "OPTIMIZED_FILE",
    "PhaseManager",
    "PhaseResult",
    "RESEARCH_FILE",
    "REVIEW_FILE",
    "REVIEW_SCORE_RE",
    "SECURITY_FILE",
    "SandboxUnavailableError",
    "TESTS_FILE",
    "WorkspaceEscapeError",
    "replace_result",
    "run_sandboxed",
    "scan_for_dangerous_calls",
    "strip_code_fences",
]
