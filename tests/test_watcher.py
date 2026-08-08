"""File watcher: change detection, restart safety, resilience."""

from __future__ import annotations

import asyncio

from looper.watcher import FileWatcher


def make_watcher(tmp_path, seen: list[str], interval: float = 0.01) -> FileWatcher:
    async def callback(content: str) -> None:
        seen.append(content)

    return FileWatcher(tmp_path / "commands.txt", callback, interval)


def test_prime_creates_the_file(tmp_path):
    watcher = make_watcher(tmp_path, [])
    watcher.prime()
    assert (tmp_path / "commands.txt").exists()


def test_prime_adopts_existing_content_as_baseline(tmp_path):
    """Without this a restart re-runs the last goal, burning API credits."""
    path = tmp_path / "commands.txt"
    path.write_text("old goal", encoding="utf-8")
    seen: list[str] = []
    watcher = make_watcher(tmp_path, seen)
    watcher.prime()
    assert asyncio.run(watcher.poll_once()) is False
    assert seen == []


def test_new_content_fires_the_callback(tmp_path):
    seen: list[str] = []
    watcher = make_watcher(tmp_path, seen)
    watcher.prime()
    (tmp_path / "commands.txt").write_text("build a CLI\n", encoding="utf-8")
    assert asyncio.run(watcher.poll_once()) is True
    assert seen == ["build a CLI"]


def test_unchanged_content_does_not_refire(tmp_path):
    seen: list[str] = []
    watcher = make_watcher(tmp_path, seen)
    watcher.prime()
    (tmp_path / "commands.txt").write_text("goal", encoding="utf-8")

    async def run():
        await watcher.poll_once()
        await watcher.poll_once()

    asyncio.run(run())
    assert seen == ["goal"]


def test_whitespace_only_content_is_ignored(tmp_path):
    seen: list[str] = []
    watcher = make_watcher(tmp_path, seen)
    watcher.prime()
    (tmp_path / "commands.txt").write_text("   \n\t ", encoding="utf-8")
    assert asyncio.run(watcher.poll_once()) is False
    assert seen == []


def test_missing_file_is_not_an_error(tmp_path):
    watcher = make_watcher(tmp_path, [])
    assert asyncio.run(watcher.poll_once()) is False


def test_read_error_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    watcher = make_watcher(tmp_path, [])
    watcher.prime()

    def boom(*args, **kwargs):
        raise OSError("locked")

    monkeypatch.setattr("pathlib.Path.read_text", boom)
    assert asyncio.run(watcher.poll_once()) is False
    assert "Watcher read error" in caplog.text


def test_prime_survives_unreadable_file(tmp_path, monkeypatch, caplog):
    watcher = make_watcher(tmp_path, [])

    def boom(*args, **kwargs):
        raise OSError("locked")

    monkeypatch.setattr("pathlib.Path.read_text", boom)
    watcher.prime()
    assert watcher._last_content == ""
    assert "Could not prime watcher" in caplog.text


def test_callback_exception_does_not_kill_the_loop(tmp_path, caplog):
    calls: list[str] = []

    async def failing(content: str) -> None:
        calls.append(content)
        raise RuntimeError("handler blew up")

    watcher = FileWatcher(tmp_path / "commands.txt", failing, interval=0.001)

    async def run():
        task = asyncio.ensure_future(watcher.start())
        await asyncio.sleep(0.02)
        (tmp_path / "commands.txt").write_text("goal", encoding="utf-8")
        await asyncio.sleep(0.05)
        watcher.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert calls == ["goal"]
    assert "Watcher callback error" in caplog.text


def test_start_stop_toggles_running(tmp_path):
    watcher = make_watcher(tmp_path, [], interval=0.001)
    assert watcher.running is False

    async def run():
        task = asyncio.ensure_future(watcher.start())
        await asyncio.sleep(0.01)
        assert watcher.running is True
        watcher.stop()
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert watcher.running is False


def test_cancellation_propagates(tmp_path):
    watcher = make_watcher(tmp_path, [], interval=10)

    async def run():
        task = asyncio.ensure_future(watcher.start())
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(run()) is True


def test_start_loop_polls_repeatedly(tmp_path):
    """Covers the sleep-and-loop body of start()."""
    seen: list[str] = []
    watcher = make_watcher(tmp_path, seen, interval=0.001)

    async def run():
        task = asyncio.ensure_future(watcher.start())
        await asyncio.sleep(0.01)
        (tmp_path / "commands.txt").write_text("first", encoding="utf-8")
        await asyncio.sleep(0.02)
        (tmp_path / "commands.txt").write_text("second", encoding="utf-8")
        await asyncio.sleep(0.02)
        watcher.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert seen == ["first", "second"]


def test_start_swallows_callback_errors_and_keeps_polling(tmp_path, caplog):
    """Covers the broad-except body: the watcher must survive a bad handler."""
    calls: list[str] = []

    async def flaky(content: str) -> None:
        calls.append(content)
        raise RuntimeError("boom")

    watcher = FileWatcher(tmp_path / "commands.txt", flaky, interval=0.001)

    async def run():
        task = asyncio.ensure_future(watcher.start())
        await asyncio.sleep(0.01)
        (tmp_path / "commands.txt").write_text("one", encoding="utf-8")
        await asyncio.sleep(0.02)
        (tmp_path / "commands.txt").write_text("two", encoding="utf-8")
        await asyncio.sleep(0.02)
        watcher.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    # Both fired despite the first raising -> the loop survived.
    assert calls == ["one", "two"]
    assert "Watcher callback error" in caplog.text


def test_start_catches_poll_errors_deterministically(tmp_path, caplog):
    """Deterministic cover for the broad-except guard in start()."""
    watcher = make_watcher(tmp_path, [], interval=0)
    attempts = {"n": 0}

    async def exploding_poll():
        attempts["n"] += 1
        if attempts["n"] >= 2:
            watcher.stop()
        raise RuntimeError("poll exploded")

    watcher.poll_once = exploding_poll
    asyncio.run(watcher.start())

    assert attempts["n"] == 2  # kept looping after the first exception
    assert "Watcher callback error" in caplog.text


def test_start_reraises_cancellation_from_poll(tmp_path):
    """Line-level cover for the `except CancelledError: raise` guard.

    Cancellation must escape the loop rather than being swallowed by the
    broad-except below it, otherwise the daemon cannot shut down cleanly.
    """
    watcher = make_watcher(tmp_path, [], interval=0)

    async def cancelled_poll():
        raise asyncio.CancelledError

    watcher.poll_once = cancelled_poll

    async def run():
        try:
            await watcher.start()
        except asyncio.CancelledError:
            return "propagated"
        return "swallowed"

    assert asyncio.run(run()) == "propagated"
