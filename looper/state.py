"""Durable daemon state with atomic, bounded persistence."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("looper.state")

DEFAULT_STATE: dict[str, Any] = {
    "current_goal": None,
    "current_phase": "idle",
    "cycle": 0,
    "score": 0.0,
    "status": "idle",
    "history": [],
    "files_created": [],
    "errors": [],
    "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
}


class StateManager:
    """Persists daemon state as JSON.

    ``update()`` mutates the in-memory dict only; call ``save()`` to flush.
    Separating the two avoids rewriting the whole file on every field change.
    ``history`` is capped at ``max_history_entries`` because the daemon runs
    24/7 and the full file is rewritten on each save - unbounded growth is an
    O(n^2) I/O and memory leak.
    """

    def __init__(self, state_file: Path, max_history_entries: int = 500) -> None:
        self.state_file = Path(state_file)
        self.max_history_entries = max_history_entries
        self.state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    merged = {**DEFAULT_STATE, **data}
                    merged["history"] = list(merged.get("history") or [])
                    merged["files_created"] = list(merged.get("files_created") or [])
                    merged["errors"] = list(merged.get("errors") or [])
                    return merged
                logger.error(
                    "State file %s holds %s, expected an object; starting fresh",
                    self.state_file,
                    type(data).__name__,
                )
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                logger.error("Corrupt state file %s: %s", self.state_file, exc)
        fresh: dict[str, Any] = json.loads(json.dumps(DEFAULT_STATE))
        return fresh

    def save(self) -> None:
        """Atomically write state to disk.

        Writes to a temp file in the same directory then ``os.replace``s it,
        so a crash mid-write can never leave a truncated state file.
        """
        parent = self.state_file.parent
        parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(
            dir=str(parent), prefix=self.state_file.name, suffix=".tmp"
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.state_file)
        except BaseException:
            with_suppressed_error = Path(tmp_name)
            if with_suppressed_error.exists():
                with_suppressed_error.unlink()
            raise

    def update(self, **kwargs: Any) -> None:
        self.state.update(kwargs)

    def append_history(self, entry: dict[str, Any]) -> None:
        history = list(self.state.get("history") or [])
        history.append(entry)
        if len(history) > self.max_history_entries:
            history = history[-self.max_history_entries :]
        self.state["history"] = history

    def record_files(self, paths: list[str]) -> None:
        files = list(self.state.get("files_created") or [])
        for path in paths:
            if path not in files:
                files.append(path)
        self.state["files_created"] = files

    def record_error(self, message: str) -> None:
        errors = list(self.state.get("errors") or [])
        errors.append(message)
        if len(errors) > self.max_history_entries:
            errors = errors[-self.max_history_entries :]
        self.state["errors"] = errors

    def reset(self) -> None:
        self.state = json.loads(json.dumps(DEFAULT_STATE))
        self.save()

    def snapshot(self) -> dict[str, Any]:
        """A JSON-safe deep copy, for serving over HTTP without data races."""
        copied: dict[str, Any] = json.loads(json.dumps(self.state))
        return copied
