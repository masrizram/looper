"""Durable daemon state with atomic, bounded persistence."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any

logger = logging.getLogger("looper.state")

#: Number of times to retry ``os.replace`` when Windows raises PermissionError.
#: Windows does not guarantee atomic rename semantics: a handle may still be
#: open on the target, or the process may lack SeCreateFilePrivilege. Retrying
#: with a short backoff usually resolves transient contention from a polling
#: reader (e.g. the HTTP /status handler holding a file handle).
_REPLACE_RETRIES = 5


def _atomic_replace(src: str, dst: str) -> None:
    """Replace ``dst`` with ``src``, retrying on Windows PermissionError.

    On POSIX ``os.replace`` is atomic and never blocks. On Windows it can
    raise PermissionError when the target is momentarily pinned by another
    handle (antivirus, a polling reader, etc.). Retrying with a short
    backoff usually resolves it; if all retries fail we fall back to a
    non-atomic copy+unlink that works everywhere, then warn loudly because
    a crash mid-copy would truncate the state file.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _REPLACE_RETRIES + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt < _REPLACE_RETRIES:
                logger.warning(
                    "os.replace retry %d/%d on %s (PermissionError on Windows): %s",
                    attempt,
                    _REPLACE_RETRIES,
                    dst,
                    exc,
                )
                time.sleep(0.01 * (2**attempt))  # 20ms, 40ms, 80ms, 160ms
        except OSError as exc:
            last_exc = exc
            break
    # Fallback: non-atomic copy. Slower and crash-unsafe, but works on
    # filesystems where os.replace cannot. If the copy itself succeeds we do
    # NOT raise -- the write completed, just not atomically. If it ALSO fails,
    # that is a genuine error we must surface.
    warnings.warn(
        f"State write fell back to non-atomic copy for {dst} after "
        f"retrying os.replace {attempt} time(s). A crash mid-write could "
        f"truncate the state file.",
        RuntimeWarning,
        stacklevel=2,
    )
    try:
        shutil.copy2(src, dst)
        logger.info("State write completed via copy2 fallback for %s", dst)
    except OSError:
        # The copy also failed; surface the original replace error as context.
        if last_exc is not None:  # pragma: no cover - last_exc is always set here
            raise last_exc


DEFAULT_STATE: dict[str, Any] = {
    "current_goal": None,
    "current_phase": "idle",
    "cycle": 0,
    "score": 0.0,
    "status": "idle",
    #: False once a build finishes below ``min_acceptable``, because the
    #: documentation and performance phases are skipped in that case and the
    #: artifact on disk is not the complete deliverable.
    "artifacts_complete": True,
    "history": [],
    "files_created": [],
    "errors": [],
    "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    #: Phase names already completed successfully for ``current_goal``, in
    #: order. This is the resume checkpoint: every entry here is a phase whose
    #: artifact is on disk and whose LLM calls have already been paid for, so
    #: ``--resume`` can skip it instead of buying the same answer twice.
    "completed_phases": [],
}


def build_checkpoint(state: dict[str, Any]) -> dict[str, Any]:
    """The subset of state that makes a run resumable.

    Split out so the contract is explicit and testable: a resume decision may
    only depend on the goal it was recorded for, the phases proven complete,
    and the cycle reached.
    """
    return {
        "goal": state.get("current_goal"),
        "completed_phases": list(state.get("completed_phases") or []),
        "cycle": int(state.get("cycle", 0) or 0),
        "status": state.get("status"),
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
        #: Cached JSON round-trip of ``state``. /status is polled far more
        #: often than the state changes, and the round-trip is O(history).
        self._snapshot_cache: dict[str, Any] | None = None

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
                    merged["completed_phases"] = list(merged.get("completed_phases") or [])
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

        On Windows, ``os.replace`` may raise ``PermissionError`` when the
        target is momentarily pinned by another handle (e.g. a /status poll).
        :func:`_atomic_replace` retries with exponential backoff and falls
        back to a non-atomic copy if all retries are exhausted.
        """
        parent = self.state_file.parent
        parent.mkdir(parents=True, exist_ok=True)
        tmp_name: str | None = None
        handle_fd = -1
        try:
            handle_fd, tmp_name = tempfile.mkstemp(
                dir=str(parent), prefix=self.state_file.name, suffix=".tmp"
            )
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            _atomic_replace(tmp_name, str(self.state_file))
        except BaseException:
            # Clean up the temp file even if the replace/copy fell back to
            # shutil.copy2 or raised. We swallow FileNotFoundError here because
            # the file may already have been removed by a finaliser.
            from contextlib import suppress

            with suppress(FileNotFoundError, TypeError):
                if tmp_name is not None:
                    Path(tmp_name).unlink(missing_ok=True)
            raise

    def update(self, **kwargs: Any) -> None:
        self.state.update(kwargs)
        self._snapshot_cache = None

    def append_history(self, entry: dict[str, Any]) -> None:
        history = list(self.state.get("history") or [])
        history.append(entry)
        if len(history) > self.max_history_entries:
            history = history[-self.max_history_entries :]
        self.state["history"] = history
        self._snapshot_cache = None

    def record_files(self, paths: list[str]) -> None:
        files = list(self.state.get("files_created") or [])
        for path in paths:
            if path not in files:
                files.append(path)
        self.state["files_created"] = files
        self._snapshot_cache = None

    def record_completed_phase(self, phase: str) -> None:
        """Mark ``phase`` as proven complete for the current goal.

        Idempotent: re-running a phase after a resume must not duplicate the
        entry, or the checkpoint would grow without bound on a daemon that
        retries. Not capped like ``history`` because the set is bounded by
        the number of known phases.
        """
        done = list(self.state.get("completed_phases") or [])
        if phase not in done:
            done.append(phase)
        self.state["completed_phases"] = done
        self._snapshot_cache = None

    def clear_completed_phases(self) -> None:
        """Drop the checkpoint (a new goal invalidates every prior artifact)."""
        self.state["completed_phases"] = []
        self._snapshot_cache = None

    def record_error(self, message: str) -> None:
        errors = list(self.state.get("errors") or [])
        errors.append(message)
        if len(errors) > self.max_history_entries:
            errors = errors[-self.max_history_entries :]
        self.state["errors"] = errors
        self._snapshot_cache = None

    def reset(self) -> None:
        self.state = json.loads(json.dumps(DEFAULT_STATE))
        self._snapshot_cache = None
        self.save()

    def snapshot(self, history_limit: int | None = None) -> dict[str, Any]:
        """A JSON-safe deep copy, for serving over HTTP without data races.

        Cached and invalidated by every mutator: /status polling used to pay
        a full O(history) serialise-plus-parse on each request.

        ``history_limit`` trims the history tail *before* the defensive copy.
        /status only ever shows the last 20 entries, so copying all 500 and
        then slicing did 25x the serialisation work per poll.
        """
        if self._snapshot_cache is None:
            self._snapshot_cache = json.loads(json.dumps(self.state))
        source = self._snapshot_cache
        if history_limit is not None:
            source = {**source, "history": source.get("history", [])[-history_limit:]}
        # Hand out a copy so a caller mutating the result cannot poison
        # the cache for the next reader.
        copied: dict[str, Any] = json.loads(json.dumps(source))
        return copied
