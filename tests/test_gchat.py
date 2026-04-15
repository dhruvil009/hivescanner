"""Tests for gchat scanner."""

import importlib.util
import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Load gchat.py by absolute path under name "gchat" so existing
# @patch("gchat.X") decorators resolve. Add workers/ (NOT workers/sources/)
# so the scanner's sibling imports resolve without shadowing stdlib.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
_GC_PATH = os.path.join(
    os.path.dirname(__file__), "..", "workers", "sources", "gchat.py"
)
_spec = importlib.util.spec_from_file_location("gchat", _GC_PATH)
_gc_mod = importlib.util.module_from_spec(_spec)
sys.modules["gchat"] = _gc_mod
_spec.loader.exec_module(_gc_mod)
GChatScanner = _gc_mod.GChatScanner


SAMPLE_DM_MESSAGE = {
    "name": "spaces/AAAA/messages/msg001",
    "sender": {
        "displayName": "Alice",
        "name": "users/12345",
    },
    "text": "Hey, can you review this?",
    "createTime": "2026-03-15T10:00:00Z",
    "space": {"type": "DM"},
    "annotations": [],
}

SAMPLE_MENTION_MESSAGE = {
    "name": "spaces/BBBB/messages/msg002",
    "sender": {
        "displayName": "Bob",
        "name": "users/67890",
    },
    "text": "Hey @dhruvil check this out",
    "createTime": "2026-03-15T11:00:00Z",
    "space": {"type": "ROOM"},
    "annotations": [{"type": "USER_MENTION"}],
}

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}


@pytest.fixture
def scanner():
    with patch("gchat.load_snapshot", return_value={}), \
         patch("gchat.save_snapshot"):
        return GChatScanner()


@pytest.fixture
def bootstrapped_scanner():
    """Scanner that has already been bootstrapped (has prior snapshot data)."""
    with patch("gchat.load_snapshot", return_value={"existing": "data"}), \
         patch("gchat.save_snapshot"):
        return GChatScanner()


class TestGChatScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["watch_spaces"] == []
        assert config["watch_dms"] is True
        assert config["username"] == ""
        assert config["max_messages"] == 20

    def test_poll_empty_when_gws_not_installed(self, scanner):
        with patch("shutil.which", return_value=None):
            scanner._cli_available = None
            pollen, wm = scanner.poll(
                {"watch_spaces": ["spaces/AAAA"]},
                "1970-01-01T00:00:00Z",
            )
        assert pollen == []
        assert wm == "1970-01-01T00:00:00Z"

    def test_poll_empty_when_no_spaces_configured(self, scanner):
        with patch("shutil.which", return_value="/usr/bin/gws"):
            scanner._cli_available = None
            pollen, wm = scanner.poll(
                {"watch_spaces": []},
                "1970-01-01T00:00:00Z",
            )
        assert pollen == []
        assert wm == "1970-01-01T00:00:00Z"

    def test_dm_message_emits_gchat_dm(self, bootstrapped_scanner):
        scanner = bootstrapped_scanner
        scanner._cli_available = True
        messages_json = json.dumps([SAMPLE_DM_MESSAGE])

        with patch.object(scanner, "_gws", return_value=messages_json), \
             patch("gchat.save_snapshot"):
            pollen, wm = scanner.poll(
                {"watch_spaces": ["spaces/AAAA"], "username": "dhruvil"},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "gchat_dm"
        assert pollen[0]["id"] == "gchat-msg001"
        assert pollen[0]["source"] == "gchat"
        assert pollen[0]["author_name"] == "Alice"

    def test_mention_annotation_emits_gchat_mention(self, bootstrapped_scanner):
        scanner = bootstrapped_scanner
        scanner._cli_available = True
        messages_json = json.dumps([SAMPLE_MENTION_MESSAGE])

        with patch.object(scanner, "_gws", return_value=messages_json), \
             patch("gchat.save_snapshot"):
            pollen, wm = scanner.poll(
                {"watch_spaces": ["spaces/BBBB"], "username": "dhruvil"},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "gchat_mention"
        assert pollen[0]["id"] == "gchat-msg002"

    def test_bootstrap_silence_emits_no_pollen(self, scanner):
        """First poll should snapshot messages without emitting pollen."""
        scanner._cli_available = True
        messages_json = json.dumps([SAMPLE_DM_MESSAGE, SAMPLE_MENTION_MESSAGE])

        with patch.object(scanner, "_gws", return_value=messages_json), \
             patch("gchat.save_snapshot"):
            pollen, wm = scanner.poll(
                {"watch_spaces": ["spaces/AAAA"], "username": "dhruvil"},
                "1970-01-01T00:00:00Z",
            )

        assert pollen == []

    def test_pollen_schema_has_all_required_keys(self, bootstrapped_scanner):
        scanner = bootstrapped_scanner
        scanner._cli_available = True
        messages_json = json.dumps([SAMPLE_DM_MESSAGE])

        with patch.object(scanner, "_gws", return_value=messages_json), \
             patch("gchat.save_snapshot"):
            pollen, wm = scanner.poll(
                {"watch_spaces": ["spaces/AAAA"], "username": "dhruvil"},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert set(pollen[0].keys()) >= REQUIRED_POLLEN_KEYS
