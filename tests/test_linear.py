"""Tests for Linear scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Load the adapter module. snapshot_store is inlined into the adapter
# (for sandboxed self-containment), so we patch load_snapshot/save_snapshot
# on the adapter module itself rather than mocking a separate module.
_LINEAR_PATH = os.path.join(
    os.path.dirname(__file__), "..", "community", "linear", "adapter.py"
)
_spec = importlib.util.spec_from_file_location("linear_adapter", _LINEAR_PATH)
_linear_mod = importlib.util.module_from_spec(_spec)
sys.modules["linear_adapter"] = _linear_mod
_spec.loader.exec_module(_linear_mod)
LinearScanner = _linear_mod.LinearScanner

# Replace the inlined load/save with mocks for tests.
mock_ss = MagicMock()
mock_ss.load_snapshot = MagicMock(return_value={})
mock_ss.save_snapshot = MagicMock()
_linear_mod.load_snapshot = mock_ss.load_snapshot
_linear_mod.save_snapshot = mock_ss.save_snapshot


SAMPLE_GRAPHQL_RESPONSE = {
    "data": {
        "issues": {
            "nodes": [
                {
                    "id": "uuid-001",
                    "identifier": "ENG-101",
                    "title": "Fix login bug",
                    "state": {"name": "In Progress"},
                    "priority": 1,
                    "assignee": {"name": "Alice", "email": "alice@example.com"},
                    "updatedAt": "2026-03-15T10:00:00Z",
                    "url": "https://linear.app/team/issue/ENG-101",
                },
            ],
        },
    },
}

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}


def _make_scanner(snapshot=None):
    """Create a LinearScanner with a controlled snapshot."""
    if snapshot is None:
        snapshot = {}
    mock_ss.load_snapshot.return_value = snapshot
    s = LinearScanner()
    return s


@pytest.fixture
def scanner():
    """Fresh scanner with empty snapshot (not yet bootstrapped)."""
    return _make_scanner({})


@pytest.fixture
def bootstrapped_scanner():
    """Scanner that already has snapshot data (bootstrapped)."""
    return _make_scanner({"ENG-100": "Done:0"})


class TestLinearScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["api_key_env"] == "LINEAR_API_KEY"
        assert config["team_id"] == ""

    def test_poll_empty_when_no_token(self, scanner):
        with patch.dict(os.environ, {}, clear=True):
            pollen, wm = scanner.poll(
                {"api_key_env": "LINEAR_API_KEY", "team_id": "team1"},
                "2026-03-15T09:00:00Z",
            )
        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"

    def test_bootstrap_silence(self, scanner):
        """First poll with empty snapshot should snapshot issues but emit no pollen."""
        with patch.dict(os.environ, {"LINEAR_API_KEY": "lin_key123"}), \
             patch.object(scanner, "_graphql", return_value=SAMPLE_GRAPHQL_RESPONSE):
            pollen, wm = scanner.poll(
                {"api_key_env": "LINEAR_API_KEY", "team_id": ""},
                "2026-03-15T09:00:00Z",
            )

        assert pollen == []
        # After bootstrap, snapshot should contain the issue
        assert "ENG-101" in scanner._snapshot

    def test_new_issue_emits_issue_assigned(self, bootstrapped_scanner):
        """A bootstrapped scanner seeing a new issue should emit issue_assigned."""
        with patch.dict(os.environ, {"LINEAR_API_KEY": "lin_key123"}), \
             patch.object(bootstrapped_scanner, "_graphql",
                          return_value=SAMPLE_GRAPHQL_RESPONSE):
            pollen, wm = bootstrapped_scanner.poll(
                {"api_key_env": "LINEAR_API_KEY", "team_id": ""},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "issue_assigned"
        # Transition hash is appended to guard against stable-id dedup eating
        # subsequent state changes. Shape: linear-<identifier>-<hash8>.
        assert p["id"].startswith("linear-ENG-101-")
        assert len(p["id"]) == len("linear-ENG-101-") + 8
        assert p["source"] == "linear"
        assert p["author"] == "alice@example.com"
        assert p["author_name"] == "Alice"
        assert p["group"] == "Issues"
        assert "ENG-101" in p["title"]

    def test_state_transitions_produce_distinct_ids(self, bootstrapped_scanner):
        """An issue moving Todo → In Progress → Done must emit distinct pollen IDs
        so add_pollen's stable-id dedup doesn't silently eat later transitions."""
        bootstrapped_scanner._snapshot["ENG-101"] = "Todo:1"

        # First transition: Todo → In Progress
        with patch.dict(os.environ, {"LINEAR_API_KEY": "k"}), \
             patch.object(bootstrapped_scanner, "_graphql", return_value=SAMPLE_GRAPHQL_RESPONSE):
            pollen1, _ = bootstrapped_scanner.poll(
                {"api_key_env": "LINEAR_API_KEY", "team_id": ""},
                "2026-03-15T09:00:00Z",
            )

        # Second transition: In Progress → Done
        bootstrapped_scanner._snapshot["ENG-101"] = "In Progress:1"
        done_response = {
            "data": {"issues": {"nodes": [
                {**SAMPLE_GRAPHQL_RESPONSE["data"]["issues"]["nodes"][0],
                 "state": {"name": "Done"}}
            ]}}
        }
        with patch.dict(os.environ, {"LINEAR_API_KEY": "k"}), \
             patch.object(bootstrapped_scanner, "_graphql", return_value=done_response):
            pollen2, _ = bootstrapped_scanner.poll(
                {"api_key_env": "LINEAR_API_KEY", "team_id": ""},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen1) == 1 and len(pollen2) == 1
        assert pollen1[0]["id"] != pollen2[0]["id"], "state transitions must yield distinct ids"

    def test_state_change_emits_issue_updated(self, bootstrapped_scanner):
        """When an already-known issue changes state, emit issue_updated."""
        # Pre-populate snapshot with the issue in a different state
        bootstrapped_scanner._snapshot["ENG-101"] = "Todo:1"

        with patch.dict(os.environ, {"LINEAR_API_KEY": "lin_key123"}), \
             patch.object(bootstrapped_scanner, "_graphql",
                          return_value=SAMPLE_GRAPHQL_RESPONSE):
            pollen, wm = bootstrapped_scanner.poll(
                {"api_key_env": "LINEAR_API_KEY", "team_id": ""},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "issue_updated"
        assert p["id"].startswith("linear-ENG-101-")
        assert p["metadata"]["state"] == "In Progress"
        assert p["metadata"]["prev_state"] == "Todo:1"

    def test_unchanged_issue_skipped(self, bootstrapped_scanner):
        """Issue with same state+priority as snapshot should be skipped."""
        # Pre-populate snapshot with the exact same state value
        bootstrapped_scanner._snapshot["ENG-101"] = "In Progress:1"

        with patch.dict(os.environ, {"LINEAR_API_KEY": "lin_key123"}), \
             patch.object(bootstrapped_scanner, "_graphql",
                          return_value=SAMPLE_GRAPHQL_RESPONSE):
            pollen, wm = bootstrapped_scanner.poll(
                {"api_key_env": "LINEAR_API_KEY", "team_id": ""},
                "2026-03-15T09:00:00Z",
            )

        assert pollen == []

    def test_pollen_schema_has_all_required_keys(self, bootstrapped_scanner):
        with patch.dict(os.environ, {"LINEAR_API_KEY": "lin_key123"}), \
             patch.object(bootstrapped_scanner, "_graphql",
                          return_value=SAMPLE_GRAPHQL_RESPONSE):
            pollen, wm = bootstrapped_scanner.poll(
                {"api_key_env": "LINEAR_API_KEY", "team_id": ""},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        missing = REQUIRED_POLLEN_KEYS - set(pollen[0].keys())
        assert not missing, f"Pollen missing keys: {missing}"
