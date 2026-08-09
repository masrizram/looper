"""State persistence: atomicity, corruption recovery, bounded growth."""

from __future__ import annotations

import json
import os

import pytest

from looper.state import DEFAULT_STATE, StateManager, _atomic_replace


def test_fresh_state_uses_defaults(tmp_path):
    sm = StateManager(tmp_path / "state.json")
    assert sm.state["status"] == "idle"
    assert sm.state["cycle"] == 0


def test_default_state_is_not_shared_between_instances(tmp_path):
    """Regression guard: a shallow copy would let one manager's history
    mutations leak into every other instance."""
    first = StateManager(tmp_path / "a.json")
    first.state["history"].append({"phase": "x"})
    second = StateManager(tmp_path / "b.json")
    assert second.state["history"] == []
    assert DEFAULT_STATE["history"] == []


def test_reset_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    sm = StateManager(path)
    sm.update(status="running", cycle=3)
    sm.save()
    sm.reset()
    assert sm.state["status"] == "idle"
    assert StateManager(path).state["cycle"] == 0


def test_save_then_reload(tmp_path):
    path = tmp_path / "state.json"
    sm = StateManager(path)
    sm.update(current_goal="build a CLI", score=88.5)
    sm.save()
    reloaded = StateManager(path)
    assert reloaded.state["current_goal"] == "build a CLI"
    assert reloaded.state["score"] == 88.5


def test_corrupt_json_recovers_to_defaults(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    sm = StateManager(path)
    assert sm.state["status"] == "idle"
    assert "Corrupt state file" in caplog.text


def test_non_object_json_recovers(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    sm = StateManager(path)
    assert sm.state["status"] == "idle"
    assert "expected an object" in caplog.text


def test_partial_state_is_merged_with_defaults(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"score": 42}), encoding="utf-8")
    sm = StateManager(path)
    assert sm.state["score"] == 42
    assert sm.state["status"] == "idle"  # filled from defaults


def test_null_collections_are_normalised(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"history": None, "files_created": None, "errors": None}), encoding="utf-8"
    )
    sm = StateManager(path)
    assert sm.state["history"] == []
    assert sm.state["files_created"] == []
    assert sm.state["errors"] == []


def test_save_creates_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "state.json"
    sm = StateManager(path)
    sm.save()
    assert path.exists()


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "state.json"
    sm = StateManager(path)
    for i in range(5):
        sm.update(cycle=i)
        sm.save()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []
    assert json.loads(path.read_text(encoding="utf-8"))["cycle"] == 4


def test_save_cleans_up_temp_on_real_failure(tmp_path, monkeypatch):
    """Temp file must be cleaned up when save genuinely fails (both replace
    and fallback copy fail)."""
    path = tmp_path / "state.json"
    sm = StateManager(path)

    def boom_replace(source, dest):
        raise OSError("disk full")

    def boom_copy(source, dest):
        raise OSError("disk full during copy")

    monkeypatch.setattr("looper.state.os.replace", boom_replace)
    monkeypatch.setattr("looper.state.shutil.copy2", boom_copy)

    with pytest.warns(RuntimeWarning, match="fell back to non-atomic copy"):
        with pytest.raises(OSError, match="disk full"):
            sm.save()
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_history_is_bounded(tmp_path):
    sm = StateManager(tmp_path / "state.json", max_history_entries=10)
    for i in range(50):
        sm.append_history({"i": i})
    history = sm.state["history"]
    assert len(history) == 10
    assert history[0]["i"] == 40  # oldest dropped, newest kept
    assert history[-1]["i"] == 49


def test_errors_are_bounded(tmp_path):
    sm = StateManager(tmp_path / "state.json", max_history_entries=3)
    for i in range(10):
        sm.record_error(f"e{i}")
    assert sm.state["errors"] == ["e7", "e8", "e9"]


def test_record_files_deduplicates(tmp_path):
    sm = StateManager(tmp_path / "state.json")
    sm.record_files(["a.py", "b.py"])
    sm.record_files(["a.py", "c.py"])
    assert sm.state["files_created"] == ["a.py", "b.py", "c.py"]


def test_update_does_not_write_to_disk(tmp_path):
    """update() is in-memory only; the old design rewrote the whole file on
    every field change, an O(n^2) I/O pattern."""
    path = tmp_path / "state.json"
    sm = StateManager(path)
    sm.update(status="running")
    assert not path.exists()
    sm.save()
    assert path.exists()


def test_snapshot_is_a_deep_copy(tmp_path):
    sm = StateManager(tmp_path / "state.json")
    sm.append_history({"phase": "build"})
    snap = sm.snapshot()
    snap["history"][0]["phase"] = "tampered"
    assert sm.state["history"][0]["phase"] == "build"


def test_unreadable_state_file_recovers(tmp_path, monkeypatch, caplog):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")

    real_open = open

    def bad_open(file, *args, **kwargs):
        if str(file) == str(path):
            raise OSError("permission denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", bad_open)
    sm = StateManager(path)
    assert sm.state["status"] == "idle"
    assert "Corrupt state file" in caplog.text


def test_save_falls_back_to_copy_when_replace_fails(tmp_path, monkeypatch):
    """When os.replace fails, save should fall back to copy2 and succeed
    with a RuntimeWarning, not raise an exception."""
    path = tmp_path / "state.json"
    sm = StateManager(path)
    sm.update(status="running", cycle=42)

    def boom(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr("looper.state.os.replace", boom)

    with pytest.warns(RuntimeWarning, match="fell back to non-atomic copy"):
        sm.save()
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["cycle"] == 42


def test_atomic_replace_retries_on_permission_error(tmp_path, monkeypatch):
    """Windows PermissionError on os.replace should be retried, not fatal."""
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    src.write_text('{"test": true}', encoding="utf-8")

    call_count = 0
    real_replace = os.replace

    def flaky_replace(source, dest):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise PermissionError("access denied")
        return real_replace(source, dest)

    monkeypatch.setattr("looper.state.os.replace", flaky_replace)
    _atomic_replace(str(src), str(dst))
    assert call_count == 3
    assert json.loads(dst.read_text(encoding="utf-8")) == {"test": True}


def test_atomic_replace_fallback_on_persistent_permission_error(tmp_path, monkeypatch):
    """If os.replace keeps failing, fall back to copy2 and warn."""
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    src.write_text("fallback data", encoding="utf-8")

    def always_fail(source, dest):
        raise PermissionError("persistent")

    monkeypatch.setattr("looper.state.os.replace", always_fail)

    with pytest.warns(RuntimeWarning, match="fell back to non-atomic copy"):
        _atomic_replace(str(src), str(dst))
    assert dst.read_text(encoding="utf-8") == "fallback data"


def test_atomic_replace_exf_posix_oserror_falls_back(tmp_path, monkeypatch):
    """OSError EXDEV (cross-device link) should trigger immediate fallback."""
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    src.write_text("cross-device", encoding="utf-8")

    def raise_exdev(source, dest):
        raise OSError("EXDEV")

    monkeypatch.setattr("looper.state.os.replace", raise_exdev)

    with pytest.warns(RuntimeWarning, match="fell back to non-atomic copy"):
        _atomic_replace(str(src), str(dst))
    assert dst.read_text(encoding="utf-8") == "cross-device"


def test_save_uses_atomic_replace_with_retry(tmp_path, monkeypatch):
    """StateManager.save should complete even if os.replace raises PermissionError
    on the first attempt, thanks to the retry in _atomic_replace."""
    path = tmp_path / "state.json"
    sm = StateManager(path)
    sm.update(status="running", cycle=1)

    call_count = 0
    real_replace = os.replace

    def flaky_replace(source, dest):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise PermissionError("access denied (simulated Windows handle contention)")
        return real_replace(source, dest)

    monkeypatch.setattr("looper.state.os.replace", flaky_replace)

    # save() should succeed on the second attempt (call_count == 2)
    sm.save()
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["cycle"] == 1
    assert call_count == 2
