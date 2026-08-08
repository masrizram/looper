"""State persistence: atomicity, corruption recovery, bounded growth."""

from __future__ import annotations

import json

import pytest

from looper.state import DEFAULT_STATE, StateManager


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


def test_save_cleans_up_temp_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    sm = StateManager(path)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("looper.state.os.replace", boom)
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


def test_save_failure_without_temp_file_still_reraises(tmp_path, monkeypatch):
    """Covers the branch where the temp file is already gone during cleanup."""
    path = tmp_path / "state.json"
    sm = StateManager(path)

    def boom(*args, **kwargs):
        raise OSError("replace failed")

    real_exists = __import__("pathlib").Path.exists
    monkeypatch.setattr("looper.state.os.replace", boom)
    monkeypatch.setattr("looper.state.Path.exists", lambda self: False)

    with pytest.raises(OSError, match="replace failed"):
        sm.save()
    assert real_exists is not None
