"""Polling file watcher that triggers builds from a text file."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

logger = logging.getLogger("looper.watcher")

Callback = Callable[[str], Awaitable[None]]


class FileWatcher:
    """Watches ``watch_file`` and fires ``callback`` when its content changes."""

    def __init__(
        self,
        watch_file: Path,
        callback: Callback,
        interval: float = 2.0,
    ) -> None:
        self.watch_file = Path(watch_file)
        self.callback = callback
        self.interval = interval
        self._last_content = ""
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def prime(self) -> None:
        """Create the file and adopt its current content as the baseline.

        Without this a daemon restart re-runs whatever goal was last left in
        the file, silently burning API credits.
        """
        self.watch_file.parent.mkdir(parents=True, exist_ok=True)
        self.watch_file.touch(exist_ok=True)
        try:
            self._last_content = self.watch_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not prime watcher from %s: %s", self.watch_file, exc)
            self._last_content = ""

    async def poll_once(self) -> bool:
        """Check the file once. Returns True if the callback fired."""
        try:
            content = self.watch_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning("Watcher read error: %s", exc)
            return False

        if not content or content == self._last_content:
            return False

        self._last_content = content
        command = content.strip()
        if not command:
            return False

        await self.callback(command)
        return True

    async def start(self) -> None:
        self._running = True
        self.prime()
        while self._running:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watcher must never die
                logger.exception("Watcher callback error")
            await asyncio.sleep(self.interval)

    def stop(self) -> None:
        self._running = False
