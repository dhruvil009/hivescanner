"""Tests for WhatsApp scanner."""

import importlib.util
import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Load whatsapp.py by absolute path under name "whatsapp" so existing
# @patch("whatsapp.X") decorators resolve. Add workers/ (NOT workers/sources/)
# so the scanner's sibling imports resolve without shadowing stdlib.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
_WA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "workers", "sources", "whatsapp.py"
)
_spec = importlib.util.spec_from_file_location("whatsapp", _WA_PATH)
_wa_mod = importlib.util.module_from_spec(_spec)
sys.modules["whatsapp"] = _wa_mod
_spec.loader.exec_module(_wa_mod)
WhatsAppScanner = _wa_mod.WhatsAppScanner


@pytest.fixture
def scanner():
    with patch("whatsapp.load_snapshot", return_value={}), \
         patch("whatsapp.save_snapshot"):
        s = WhatsAppScanner()
        # Mark as already bootstrapped so polls emit pollen by default
        s._bootstrapped = True
        return s


@pytest.fixture
def bootstrapping_scanner():
    """Scanner that has NOT been bootstrapped yet (first poll)."""
    with patch("whatsapp.load_snapshot", return_value={}), \
         patch("whatsapp.save_snapshot"):
        s = WhatsAppScanner()
        return s


SAMPLE_MESSAGES = [
    {
        "id": "msg001",
        "chat_jid": "group-abc@g.us",
        "sender": "14155551234@s.whatsapp.net",
        "sender_name": "Alice",
        "content": "Hello everyone!",
        "timestamp": "2026-03-15T10:00:00Z",
        "media_type": "",
    },
    {
        "id": "msg002",
        "chat_jid": "group-xyz@g.us",
        "sender": "14155555678@s.whatsapp.net",
        "sender_name": "Bob",
        "content": "Check this out",
        "timestamp": "2026-03-15T11:00:00Z",
        "media_type": "image",
    },
]

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name", "group", "url", "metadata",
}


class TestWhatsAppScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["watch_chats"] == []
        assert config["max_messages"] == 20

    @patch("whatsapp.ensure_tool", return_value=False)
    @patch("whatsapp.save_snapshot")
    def test_poll_no_cli(self, _save, _ensure, scanner):
        scanner._cli_available = None
        config = scanner.configure()
        pollen, wm = scanner.poll(config, "")
        assert pollen == []

    @patch("whatsapp.ensure_tool", return_value=True)
    @patch("whatsapp.save_snapshot")
    def test_poll_no_messages(self, _save, _ensure, scanner):
        scanner._cli_available = None
        with patch.object(scanner, "_wa", return_value=json.dumps([])):
            config = scanner.configure()
            pollen, wm = scanner.poll(config, "")
            assert pollen == []

    @patch("whatsapp.ensure_tool", return_value=True)
    @patch("whatsapp.save_snapshot")
    def test_poll_new_message(self, _save, _ensure, scanner):
        scanner._cli_available = None
        with patch.object(scanner, "_wa", return_value=json.dumps(SAMPLE_MESSAGES[:1])):
            config = scanner.configure()
            pollen, wm = scanner.poll(config, "")
            assert len(pollen) == 1
            p = pollen[0]
            assert p["type"] == "whatsapp_message"
            assert p["id"] == "whatsapp-msg001"
            assert p["source"] == "whatsapp"
            assert p["author_name"] == "Alice"

    @patch("whatsapp.ensure_tool", return_value=True)
    @patch("whatsapp.save_snapshot")
    def test_watch_chats_filtering(self, _save, _ensure, scanner):
        scanner._cli_available = None
        with patch.object(scanner, "_wa", return_value=json.dumps(SAMPLE_MESSAGES)):
            config = scanner.configure()
            config["watch_chats"] = ["group-abc@g.us"]
            pollen, wm = scanner.poll(config, "")
            assert len(pollen) == 1
            assert pollen[0]["group"] == "group-abc@g.us"

    @patch("whatsapp.ensure_tool", return_value=True)
    @patch("whatsapp.save_snapshot")
    def test_bootstrap_silence(self, _save, _ensure, bootstrapping_scanner):
        bootstrapping_scanner._cli_available = None
        with patch.object(bootstrapping_scanner, "_wa", return_value=json.dumps(SAMPLE_MESSAGES)):
            config = bootstrapping_scanner.configure()
            pollen, wm = bootstrapping_scanner.poll(config, "")
            assert pollen == []

    @patch("whatsapp.ensure_tool", return_value=True)
    @patch("whatsapp.save_snapshot")
    def test_pollen_schema(self, _save, _ensure, scanner):
        scanner._cli_available = None
        with patch.object(scanner, "_wa", return_value=json.dumps(SAMPLE_MESSAGES)):
            config = scanner.configure()
            pollen, wm = scanner.poll(config, "")
            assert len(pollen) > 0
            for p in pollen:
                assert REQUIRED_POLLEN_KEYS.issubset(p.keys()), \
                    f"Missing keys: {REQUIRED_POLLEN_KEYS - p.keys()}"
