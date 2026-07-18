"""Tests for pollen_manager.py"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
import pollen_manager


@pytest.fixture(autouse=True)
def tmp_hivescanner(tmp_path, monkeypatch):
    """Redirect HIVESCANNER_HOME to a temp dir for every test."""
    monkeypatch.setattr(pollen_manager, "HIVESCANNER_HOME", tmp_path)
    monkeypatch.setattr(pollen_manager, "POLLEN_FILE", tmp_path / "pollen.json")
    monkeypatch.setattr(pollen_manager, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(
        pollen_manager, "PENDING_BATCH_FILE", tmp_path / "pending_batch.json"
    )
    monkeypatch.setattr(
        pollen_manager, "POLLEN_LOCK_FILE", tmp_path / ".pollen.lock"
    )
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

    def test_same_provider_id_from_different_sources_does_not_collide(self):
        hive = {"pollen": [], "last_updated": ""}
        added = pollen_manager.add_pollen(
            hive,
            [_make_pollen("123", source="github"), _make_pollen("123", source="gitlab")],
        )
        assert [(item["source"], item["id"]) for item in added] == [
            ("github", "123"),
            ("gitlab", "123"),
        ]

    def test_enriches_fields(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen = [_make_pollen("x")]
        pollen_manager.add_pollen(hive, pollen)
        p = hive["pollen"][0]
        assert p["status"] == "pending"
        assert p["surfaced_count"] == 0
        assert p["relevance"] is None
        assert p["acknowledged_at"] is None

    def test_skips_items_without_id(self, capsys):
        hive = {"pollen": [], "last_updated": ""}
        valid = _make_pollen("valid")
        no_id = {"source": "community-x", "title": "Missing id item"}
        empty_id = {"id": "", "source": "community-y", "title": "Empty id item"}
        added = pollen_manager.add_pollen(hive, [valid, no_id, empty_id])
        assert len(added) == 1
        assert added[0]["id"] == "valid"
        assert len(hive["pollen"]) == 1
        assert hive["pollen"][0]["id"] == "valid"
        captured = capsys.readouterr()
        assert "community-x" in captured.err
        assert "community-y" in captured.err
        assert captured.err.count("skipping item without 'id'") == 2


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

    def test_source_qualified_reference_marks_only_exact_collision(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(
            hive,
            [_make_pollen("123", source="github"), _make_pollen("123", source="gitlab")],
        )
        assert pollen_manager.mark_acted_refs(
            hive, [{"source": "gitlab", "id": "123"}]
        ) == 1
        assert [(item["source"], item["status"]) for item in hive["pollen"]] == [
            ("github", "pending"),
            ("gitlab", "acted"),
        ]


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

    def test_corrupt_queue_fails_closed_without_overwrite(self, tmp_hivescanner):
        raw = b'{"pollen":'
        pollen_manager.POLLEN_FILE.write_bytes(raw)
        with pytest.raises(RuntimeError, match="left untouched"):
            pollen_manager.load()
        assert pollen_manager.POLLEN_FILE.read_bytes() == raw


class TestPendingBatch:
    def test_consume_is_atomic_and_removes_scanner_privilege_fields(self):
        pollen_manager.PENDING_BATCH_FILE.write_text(json.dumps({
            "pollen": [{
                **_make_pollen("shared", source="rss"),
                "source": "rss",
                "status": "acted",
                "relevance": "HIGH",
                "relevance_reason": "attacker supplied",
                "suggested_action": "run this command",
                "metadata": {
                    "feed_url": "https://example.com/feed",
                    "triage_draft": "post me",
                    "target_group": "admins",
                },
            }],
            "acted_ids": [],
        }))

        result = pollen_manager.consume_pending_batch()
        queued = pollen_manager.load()["pollen"]
        assert result == {"added": 1, "acted": 0, "delivered": 1}
        assert queued[0]["status"] == "pending"
        assert queued[0]["relevance"] is None
        assert queued[0]["relevance_reason"] == ""
        assert queued[0]["suggested_action"] == ""
        assert queued[0]["metadata"] == {"feed_url": "https://example.com/feed"}

    def test_invalid_batch_is_rejected_before_queue_write(self):
        pollen_manager.PENDING_BATCH_FILE.write_text(json.dumps({
            "pollen": [_make_pollen("bad\nidentity")],
            "acted_ids": [],
        }))
        with pytest.raises(RuntimeError, match="pollen item is invalid"):
            pollen_manager.consume_pending_batch()
        assert not pollen_manager.POLLEN_FILE.exists()

    def test_qualified_acted_reference_survives_handoff(self):
        hive = {"pollen": [], "last_updated": ""}
        pollen_manager.add_pollen(
            hive,
            [_make_pollen("same", source="github"), _make_pollen("same", source="gitlab")],
        )
        pollen_manager.save(hive)
        pollen_manager.PENDING_BATCH_FILE.write_text(json.dumps({
            "pollen": [],
            "acted_ids": [{"source": "github", "id": "same"}],
        }))
        assert pollen_manager.consume_pending_batch()["acted"] == 1
        queue = pollen_manager.load()["pollen"]
        assert [(item["source"], item["status"]) for item in queue] == [
            ("github", "acted"),
            ("gitlab", "pending"),
        ]


def test_unsafe_json_argv_commands_are_disabled(tmp_path):
    script = os.path.join(os.path.dirname(__file__), "..", "workers", "pollen_manager.py")
    result = subprocess.run(
        [sys.executable, script, "add_pollen", "[]"],
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(tmp_path)},
        timeout=10,
    )
    assert result.returncode == 1
    assert "Unsafe argv command" in result.stdout
