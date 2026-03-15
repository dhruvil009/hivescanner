"""Tests for Telegram scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch

import pytest

# Load the telegram adapter explicitly by file path to avoid collision
# with other community adapters that share the module name "adapter".
_adapter_path = os.path.join(os.path.dirname(__file__), "..", "community", "telegram", "adapter.py")
_spec = importlib.util.spec_from_file_location("telegram_adapter", _adapter_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
TelegramScanner = _mod.TelegramScanner

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}

SAMPLE_UPDATE = {
    "update_id": 100001,
    "message": {
        "chat": {"id": 12345, "title": "Dev Team"},
        "from": {"username": "alice", "first_name": "Alice"},
        "text": "Hello everyone",
    },
}

SAMPLE_MENTION_UPDATE = {
    "update_id": 100002,
    "message": {
        "chat": {"id": 12345, "title": "Dev Team"},
        "from": {"username": "bob", "first_name": "Bob"},
        "text": "Hey @mybot check this",
    },
}

SAMPLE_REPLY_UPDATE = {
    "update_id": 100003,
    "message": {
        "chat": {"id": 12345, "title": "Dev Team"},
        "from": {"username": "carol", "first_name": "Carol"},
        "text": "Sure, will do",
        "reply_to_message": {
            "message_id": 999,
            "text": "Original message",
        },
    },
}

GET_ME_RESPONSE = {
    "ok": True,
    "result": {"username": "mybot"},
}


@pytest.fixture
def scanner():
    return TelegramScanner()


class TestTelegramScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["token_env"] == "TELEGRAM_BOT_TOKEN"
        assert config["watch_chats"] == []
        assert config["max_messages"] == 20

    def test_poll_empty_when_no_token(self, scanner, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        pollen, wm = scanner.poll(
            {"token_env": "TELEGRAM_BOT_TOKEN", "watch_chats": [], "max_messages": 20},
            "0",
        )
        assert pollen == []
        assert wm == "0"

    def test_message_emits_telegram_message(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")

        def fake_api(method, token, params=None):
            if method == "getUpdates":
                return {"ok": True, "result": [SAMPLE_UPDATE]}
            if method == "getMe":
                return GET_ME_RESPONSE
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "TELEGRAM_BOT_TOKEN", "watch_chats": [], "max_messages": 20},
                "0",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "telegram_message"
        assert p["source"] == "telegram"
        assert p["id"] == "telegram-100001"
        assert p["author"] == "alice"
        assert p["author_name"] == "Alice"
        assert p["group"] == "Dev Team"
        assert p["preview"] == "Hello everyone"

    def test_mention_emits_telegram_mention(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")

        def fake_api(method, token, params=None):
            if method == "getUpdates":
                return {"ok": True, "result": [SAMPLE_MENTION_UPDATE]}
            if method == "getMe":
                return GET_ME_RESPONSE
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "TELEGRAM_BOT_TOKEN", "watch_chats": [], "max_messages": 20},
                "0",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "telegram_mention"
        assert p["author"] == "bob"
        assert "@mybot" in SAMPLE_MENTION_UPDATE["message"]["text"]

    def test_reply_emits_telegram_mention(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")

        def fake_api(method, token, params=None):
            if method == "getUpdates":
                return {"ok": True, "result": [SAMPLE_REPLY_UPDATE]}
            if method == "getMe":
                return GET_ME_RESPONSE
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "TELEGRAM_BOT_TOKEN", "watch_chats": [], "max_messages": 20},
                "0",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "telegram_mention"
        assert p["author"] == "carol"

    def test_watch_chats_filtering(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")

        other_chat_update = {
            "update_id": 100004,
            "message": {
                "chat": {"id": 99999, "title": "Other Chat"},
                "from": {"username": "dave", "first_name": "Dave"},
                "text": "Message in wrong chat",
            },
        }

        def fake_api(method, token, params=None):
            if method == "getUpdates":
                return {"ok": True, "result": [SAMPLE_UPDATE, other_chat_update]}
            if method == "getMe":
                return GET_ME_RESPONSE
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "TELEGRAM_BOT_TOKEN", "watch_chats": [12345], "max_messages": 20},
                "0",
            )

        assert len(pollen) == 1
        assert pollen[0]["author"] == "alice"
        assert pollen[0]["group"] == "Dev Team"

    def test_watermark_advances_to_highest_update_id(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")

        def fake_api(method, token, params=None):
            if method == "getUpdates":
                return {"ok": True, "result": [SAMPLE_UPDATE, SAMPLE_MENTION_UPDATE]}
            if method == "getMe":
                return GET_ME_RESPONSE
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "TELEGRAM_BOT_TOKEN", "watch_chats": [], "max_messages": 20},
                "0",
            )

        assert len(pollen) == 2
        assert wm == "100002"

    def test_pollen_schema_has_all_required_keys(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")

        def fake_api(method, token, params=None):
            if method == "getUpdates":
                return {"ok": True, "result": [SAMPLE_UPDATE]}
            if method == "getMe":
                return GET_ME_RESPONSE
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "TELEGRAM_BOT_TOKEN", "watch_chats": [], "max_messages": 20},
                "0",
            )

        assert len(pollen) == 1
        assert set(pollen[0].keys()) >= REQUIRED_POLLEN_KEYS
