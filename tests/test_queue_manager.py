"""Tests for pollen_manager.py"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
import pollen_manager


@pytest.fixture(autouse=True)
def tmp_hivescanner(tmp_path, monkeypatch):
    """Redirect HIVESCANNER_HOME to a temp dir for every test."""
    monkeypatch.setattr(pollen_manager, "HIVESCANNER_HOME", tmp_path)
    monkeypatch.setattr(pollen_manager, "POLLEN_FILE", tmp_path / "pollen.json")
    return tmp_path


def _make_pollen(id: str, source: str = "github", type: str = "review_needed", **kwargs) -> dict:
    p = {
        "id": id,
        "source": source,
        "type": type,
        "title": f"Test pollen {id}",
        "preview": f"Preview for {id}",
        "discovered_at": "2026-03-14T10:00:00Z",
        "author": "testuser",
        "author_name": "Test User",
        "group": "Test",
        "url": "",
        "metadata": {},
    }
    p.update(kwargs)
    return p


class TestAddPollen:
    def test_adds_new_pollen(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen = [_make_pollen("a"), _make_pollen("b")]
        added = pollen_manager.add_pollen(hive, pollen)
        assert len(added) == 2
        assert len(hive["pollen"]) == 2
        assert hive["pollen"][0]["status"] == "pending"
        assert hive["pollen"][0]["surfaced_count"] == 0

    def test_deduplicates(self):
        hive = {"pollen": [_make_pollen("a")], "last_updated": ""}
        hive["pollen"][0]["status"] = "pending"
        added = pollen_manager.add_pollen(hive, [_make_pollen("a"), _make_pollen("b")])
        assert len(added) == 1
        assert added[0]["id"] == "b"
        assert len(hive["pollen"]) == 2

    def test_enriches_fields(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen = [_make_pollen("x")]
        pollen_manager.add_pollen(hive, pollen)
        p = hive["pollen"][0]
        assert p["status"] == "pending"
        assert p["surfaced_count"] == 0
        assert p["relevance"] is None
        assert p["acknowledged_at"] is None


class TestDismiss:
    def test_dismiss_by_id(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(hive, [_make_pollen("a"), _make_pollen("b")])
        count = pollen_manager.dismiss(hive, ["a"])
        assert count == 1
        assert hive["pollen"][0]["status"] == "acknowledged"
        assert hive["pollen"][1]["status"] == "pending"

    def test_dismiss_by_number(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(hive, [
            _make_pollen("a", discovered_at="2026-03-14T10:00:00Z"),
            _make_pollen("b", discovered_at="2026-03-14T11:00:00Z"),
            _make_pollen("c", discovered_at="2026-03-14T12:00:00Z"),
        ])
        count = pollen_manager.dismiss_by_number(hive, [2])
        assert count == 1
        # Number 2 = second in discovered_at order = "b"
        for p in hive["pollen"]:
            if p["id"] == "b":
                assert p["status"] == "acknowledged"
            else:
                assert p["status"] == "pending"

    def test_dismiss_all(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(hive, [_make_pollen("a"), _make_pollen("b")])
        count = pollen_manager.dismiss_all(hive)
        assert count == 2
        assert all(p["status"] == "acknowledged" for p in hive["pollen"])


class TestGetPending:
    def test_returns_only_pending(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(hive, [_make_pollen("a"), _make_pollen("b")])
        pollen_manager.dismiss(hive, ["a"])
        pending = pollen_manager.get_pending(hive)
        assert len(pending) == 1
        assert pending[0]["id"] == "b"

    def test_sorted_by_discovered_at(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(hive, [
            _make_pollen("late", discovered_at="2026-03-14T12:00:00Z"),
            _make_pollen("early", discovered_at="2026-03-14T08:00:00Z"),
            _make_pollen("mid", discovered_at="2026-03-14T10:00:00Z"),
        ])
        pending = pollen_manager.get_pending(hive)
        assert [p["id"] for p in pending] == ["early", "mid", "late"]


class TestMarkActed:
    def test_marks_acted(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(hive, [_make_pollen("a")])
        count = pollen_manager.mark_acted(hive, ["a"])
        assert count == 1
        assert hive["pollen"][0]["status"] == "acted"
        assert hive["pollen"][0]["acted_at"] is not None


class TestPrune:
    def test_prunes_old_acknowledged(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(hive, [_make_pollen("a")])
        pollen_manager.dismiss(hive, ["a"])
        # Set acknowledged_at to 10 days ago
        hive["pollen"][0]["acknowledged_at"] = "2020-01-01T00:00:00Z"
        pruned = pollen_manager.prune(hive, retention_days=7)
        assert pruned == 1
        assert len(hive["pollen"]) == 0

    def test_never_prunes_pending(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(hive, [_make_pollen("a", discovered_at="2020-01-01T00:00:00Z")])
        pruned = pollen_manager.prune(hive, retention_days=7)
        assert pruned == 0
        assert len(hive["pollen"]) == 1


class TestStats:
    def test_counts(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(hive, [_make_pollen("a"), _make_pollen("b"), _make_pollen("c")])
        pollen_manager.dismiss(hive, ["a"])
        pollen_manager.mark_acted(hive, ["b"])
        s = pollen_manager.stats(hive)
        assert s == {"total": 3, "pending": 1, "acknowledged": 1, "acted": 1}


class TestSaveLoad:
    def test_roundtrip(self, tmp_hivescanner):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(hive, [_make_pollen("a")])
        pollen_manager.save(hive)

        loaded = pollen_manager.load()
        assert len(loaded["pollen"]) == 1
        assert loaded["pollen"][0]["id"] == "a"

    def test_load_pollen_ids(self, tmp_hivescanner):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(hive, [_make_pollen("a"), _make_pollen("b")])
        pollen_manager.dismiss(hive, ["a"])
        pollen_manager.save(hive)

        ids = pollen_manager.load_pollen_ids()
        assert ids == {"a", "b"}
