"""Workspace filesystem sink: path containment and size-capped writes.

This is the only place LLM-influenced names become real paths, so the
containment rules live here rather than being restated at each call site
(ADR-001). Splitting it out of the phase logic also means the escape rules
can be reviewed and tested without spinning up an agent pipeline.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from looper.config import LooperConfig
from looper.phases.results import WorkspaceEscapeError
from looper.state import StateManager

logger = logging.getLogger("looper.phases")

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
#: would always raise, so a fenced block's body is extracted; if there is no
#: fence the text is returned unchanged (the builder may emit bare code).
_FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)

#: Fence languages that denote Python source.
_PYTHON_FENCE_LANGS: frozenset[str] = frozenset({"", "py", "python", "python3"})


def strip_code_fences(text: str) -> str:
    """Return the body of the most plausible code block, or ``text`` unchanged.

    A non-anchored search (not ``match``) is deliberate: a reply such as
    "Here is the code:\\n```python\\nx = 1\\n```" must yield ``x = 1``, not the
    whole fenced string, otherwise ``ast.parse`` rejects valid code and the
    build fails closed for no reason.

    Taking the *first* block was wrong for the common reply shape "here is a
    small example ... and here is the full module": the throwaway snippet was
    written to disk and the real artifact silently discarded. Python-tagged
    blocks are preferred, and among candidates the longest wins, because the
    module under construction is essentially never the shortest block in the
    reply.
    """
    blocks: list[tuple[str, str]] = _FENCE_RE.findall(text or "")
    if not blocks:
        return text or ""
    python_blocks = [body for lang, body in blocks if lang.strip().lower() in _PYTHON_FENCE_LANGS]
    candidates = python_blocks or [body for _, body in blocks]
    return max(candidates, key=len).strip()


class WorkspaceMixin:
    """Path containment and capped writes for the workspace."""

    config: LooperConfig
    state: StateManager
    workspace: Path

    #: Relative paths whose most recent write hit ``max_file_bytes``. Declared
    #: for typing only; each instance gets its own set on first write (a
    #: class-level mutable would be shared by every PhaseManager).
    _truncated_paths: set[str]

    def _truncation_log(self) -> set[str]:
        """This instance's truncation set, created on first use."""
        existing = self.__dict__.get("_truncated_paths")
        if existing is None:
            existing = set()
            self.__dict__["_truncated_paths"] = existing
        return existing

    def resolve_in_workspace(self, relative_path: str) -> Path:
        """Resolve ``relative_path`` inside the workspace, or refuse.

        This is the single filesystem sink for LLM-influenced names, so the
        containment check belongs here rather than at each call site. A
        ``..`` check alone is not enough: a symlinked directory component
        (planted by an earlier cycle, or by anything else with write access
        to the workspace) redirects the write outside the root while the
        resolved-path comparison still looks clean on some platforms.
        """
        root = self.workspace.resolve()
        candidate = (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise WorkspaceEscapeError(f"Path escapes workspace: {relative_path!r}")
        probe = candidate
        while probe != root:
            if probe.is_symlink():
                raise WorkspaceEscapeError(f"Symlinked path component refused: {probe}")
            probe = probe.parent
        return candidate

    def write_file(self, relative_path: str, content: str) -> str:
        """Write agent output into the workspace, size-capped.

        Prose artifacts are truncated rather than rejected: a partial research
        note is more useful to the next phase than none, and the marker makes
        the truncation obvious to both the reviewer agent and a human.

        Python artifacts are different. A truncated module is usually still
        *parseable* -- it just silently loses its last functions -- so
        ``build_ok`` would describe a file the builder never actually wrote.
        The truncation marker is a comment, so it cannot even fail the lint
        gate. The caller is told via :meth:`last_write_truncated` so the build
        can fail closed instead.
        """
        path = self.resolve_in_workspace(relative_path)
        limit = self.config.execution.max_file_bytes
        encoded = content.encode("utf-8")
        truncated = len(encoded) > limit
        if truncated:
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
        self._truncation_log()
        if truncated:
            self._truncated_paths.add(relative_path)
        else:
            self._truncated_paths.discard(relative_path)
        self.state.record_files([str(path)])
        return str(path)

    def was_truncated(self, relative_path: str) -> bool:
        """True when the last write to ``relative_path`` hit the size cap."""
        return relative_path in self._truncation_log()

    def read_file(self, relative_path: str) -> str:
        path = self.resolve_in_workspace(relative_path)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""
