"""Tests for snapshot_store.py"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
import snapshot_store


@pytest.fixture(autouse=True)
def tmp_hivescanner(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_store, "HIVESCANNER_HOME", tmp_path)
    monkeypatch.setattr(snapshot_store, "SNAPSHOTS_FILE", tmp_path / "snapshots.json")
    return tmp_path


class TestSnapshots:
    def test_load_missing(self):
        assert snapshot_store.load_snapshot("nonexistent") == {}

    def test_save_and_load(self):
        snapshot_store.save_snapshot("test", {"key": "value"})
        result = snapshot_store.load_snapshot("test")
        assert result == {"key": "value"}

    def test_multiple_snapshots(self):
        snapshot_store.save_snapshot("a", {"x": 1})
        snapshot_store.save_snapshot("b", {"y": 2})
        assert snapshot_store.load_snapshot("a") == {"x": 1}
        assert snapshot_store.load_snapshot("b") == {"y": 2}

    def test_overwrite(self):
        snapshot_store.save_snapshot("test", {"v": 1})
        snapshot_store.save_snapshot("test", {"v": 2})
        assert snapshot_store.load_snapshot("test") == {"v": 2}
