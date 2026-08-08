"""Multi-file artifact extraction from a builder agent's reply.

``single_file`` mode can only ever produce a toy: one ``generated_code.py``
is not software, and the reviewer/security agents see a file that a real
project would have split. ``package`` mode lets the builder emit a small file
tree using an explicit, unambiguous marker syntax (ADR-009)::

    ### FILE: src/app/models.py
    ```python
    ...
    ```

Everything here is parsing of attacker-influenced text, so it is deliberately
strict: unknown syntax yields no files (the caller falls back to single-file
mode) rather than guessing, and path safety is re-checked by the caller's
workspace containment sink.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("looper.artifact")

#: ``### FILE: path`` (also tolerates ``## FILE:`` / ``#### File:``). Backticks
#: are allowed in the capture and stripped afterwards, because agents often
#: write the path as ``### FILE: `src/app.py` ``.
FILE_MARKER_RE = re.compile(r"^#{2,4}\s*FILE:\s*(?P<path>[^\n]+?)\s*$", re.IGNORECASE | re.M)

#: Extensions we are willing to write from agent output.
ALLOWED_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".md", ".txt", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml"}
)

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


@dataclass(frozen=True, slots=True)
class ArtifactFile:
    path: str
    content: str

    @property
    def is_python(self) -> bool:
        return self.path.endswith(".py")


def _clean_body(raw: str) -> str:
    """Return the fenced body when present, else the raw text, stripped."""
    match = _FENCE_RE.search(raw)
    return (match.group(1) if match else raw).strip()


def is_safe_relative_path(path: str) -> bool:
    """Reject absolute paths, traversal, drive letters and odd suffixes.

    The workspace sink re-validates containment; this is the cheap first gate
    that also keeps the artifact tree tidy and predictable.
    """
    if not path or path.startswith(("/", "\\")) or ".." in path.split("/"):
        return False
    if re.match(r"^[A-Za-z]:", path):
        return False
    if "\x00" in path or path.endswith("/"):
        return False
    suffix = path[path.rfind(".") :] if "." in path else ""
    return suffix.lower() in ALLOWED_SUFFIXES


def parse_multifile(text: str, *, max_files: int) -> list[ArtifactFile]:
    """Split a builder reply into files. Empty list == not multi-file output.

    Files beyond ``max_files`` are dropped with a warning rather than written:
    an agent that loops must not be able to fill the disk of a 24/7 daemon.
    """
    markers = list(FILE_MARKER_RE.finditer(text or ""))
    if not markers:
        return []

    files: list[ArtifactFile] = []
    for index, marker in enumerate(markers):
        start = marker.end()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        path = marker.group("path").strip().strip("`").replace("\\", "/")
        if not is_safe_relative_path(path):
            logger.warning("Dropping unsafe artifact path from agent output: %r", path)
            continue
        body = _clean_body(text[start:end])
        if not body:
            continue
        if len(files) >= max_files:
            logger.warning("Agent emitted more than %d files; dropping the rest", max_files)
            break
        files.append(ArtifactFile(path=path, content=body))
    return files


def verify_python_files(files: list[ArtifactFile]) -> tuple[bool, str]:
    """Every ``.py`` artifact must parse; otherwise the build fails closed.

    Mirrors the single-file ``_verify_syntax`` guarantee: ``build_ok`` means
    the code parses, not that the model replied.
    """
    python_files = [f for f in files if f.is_python]
    if not python_files:
        return False, "package build produced no Python files"
    for artifact in python_files:
        try:
            ast.parse(artifact.content)
        except SyntaxError as exc:
            return False, f"{artifact.path} has a syntax error: {exc}"
    return True, ""


def primary_module(files: list[ArtifactFile]) -> str:
    """Concatenate Python sources for the reviewer/security agents.

    Those agents take a single blob of code; giving them only the first file
    would let vulnerabilities in the other modules pass unaudited.
    """
    return "\n\n".join(f"# ==== {f.path} ====\n{f.content}" for f in files if f.is_python)
