"""Tests for Facebook scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_fb_adapter_path = os.path.join(os.path.dirname(__file__), "..", "community", "facebook", "adapter.py")
_spec = importlib.util.spec_from_file_location("fb_adapter", _fb_adapter_path)
_fb_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fb_mod)
FacebookScanner = _fb_mod.FacebookScanner

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}

SAMPLE_NOTIFICATION = {
    "id": "notif_001",
    "title": "Alice commented on your post",
    "created_time": "2026-03-15T10:00:00+0000",
    "from": {"id": "111", "name": "Alice"},
    "link": "https://facebook.com/notifications/001",
    "application": {"name": "Facebook"},
}

SAMPLE_OLD_NOTIFICATION = {
    "id": "notif_old",
    "title": "Old notification",
    "created_time": "2026-03-14T08:00:00+0000",
    "from": {"id": "222", "name": "Bob"},
    "link": "https://facebook.com/notifications/old",
    "application": {"name": "Facebook"},
}

SAMPLE_CONVERSATION = {
    "id": "conv_001",
    "messages": {
        "data": [
            {
                "message": "Hey, how are you?",
                "from": {"id": "333", "name": "Charlie"},
                "created_time": "2026-03-15T11:00:00+0000",
            },
        ],
    },
}

SAMPLE_OLD_CONVERSATION = {
    "id": "conv_002",
    "messages": {
        "data": [
            {
                "message": "Old message",
                "from": {"id": "444", "name": "Dave"},
                "created_time": "2026-03-14T07:00:00+0000",
            },
        ],
    },
}


@pytest.fixture
def scanner():
    return FacebookScanner()


class TestFacebookScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["token_env"] == "FACEBOOK_TOKEN"
        assert config["watch_pages"] == []
        assert config["max_items"] == 20

    def test_poll_empty_when_no_token(self, scanner, monkeypatch):
        monkeypatch.delenv("FACEBOOK_TOKEN", raising=False)
        pollen, wm = scanner.poll(
            {"token_env": "FACEBOOK_TOKEN", "watch_pages": [], "max_items": 20},
            "2026-03-15T09:00:00Z",
        )
        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"

    def test_notification_emits_facebook_notification(self, scanner, monkeypatch):
        monkeypatch.setenv("FACEBOOK_TOKEN", "fake-fb-token")

        def fake_graph(endpoint, token, params=None):
            if endpoint == "/me/notifications":
                return {"data": [SAMPLE_NOTIFICATION]}
            if endpoint == "/me/conversations":
                return {"data": []}
            return None

        with patch.object(scanner, "_graph", side_effect=fake_graph):
            pollen, wm = scanner.poll(
                {"token_env": "FACEBOOK_TOKEN", "max_items": 20},
                "2026-03-14T00:00:00Z",
            )

        notifs = [p for p in pollen if p["type"] == "facebook_notification"]
        assert len(notifs) == 1
        assert notifs[0]["source"] == "facebook"
        assert notifs[0]["id"] == "facebook-notif-notif_001"
        assert notifs[0]["author_name"] == "Alice"
        assert notifs[0]["group"] == "Notifications"

    def test_message_emits_facebook_message(self, scanner, monkeypatch):
        monkeypatch.setenv("FACEBOOK_TOKEN", "fake-fb-token")

        def fake_graph(endpoint, token, params=None):
            if endpoint == "/me/notifications":
                return {"data": []}
            if endpoint == "/me/conversations":
                return {"data": [SAMPLE_CONVERSATION]}
            return None

        with patch.object(scanner, "_graph", side_effect=fake_graph):
            pollen, wm = scanner.poll(
                {"token_env": "FACEBOOK_TOKEN", "max_items": 20},
                "2026-03-14T00:00:00Z",
            )

        msgs = [p for p in pollen if p["type"] == "facebook_message"]
        assert len(msgs) == 1
        assert msgs[0]["source"] == "facebook"
        assert msgs[0]["author_name"] == "Charlie"
        assert msgs[0]["group"] == "Messenger"
        assert "Hey, how are you?" in msgs[0]["preview"]

    def test_watermark_filters_old_items(self, scanner, monkeypatch):
        monkeypatch.setenv("FACEBOOK_TOKEN", "fake-fb-token")

        # Watermark is set after the old items but before the new ones
        watermark = "2026-03-15T00:00:00+0000"

        def fake_graph(endpoint, token, params=None):
            if endpoint == "/me/notifications":
                return {"data": [SAMPLE_NOTIFICATION, SAMPLE_OLD_NOTIFICATION]}
            if endpoint == "/me/conversations":
                return {"data": [SAMPLE_CONVERSATION, SAMPLE_OLD_CONVERSATION]}
            return None

        with patch.object(scanner, "_graph", side_effect=fake_graph):
            pollen, wm = scanner.poll(
                {"token_env": "FACEBOOK_TOKEN", "max_items": 20},
                watermark,
            )

        # Old notification (2026-03-14T08:00:00) and old message (2026-03-14T07:00:00) should be filtered
        assert all(p["type"] in ("facebook_notification", "facebook_message") for p in pollen)
        assert len(pollen) == 2  # Only the new notification + new message

    def test_pollen_schema_has_all_required_keys(self, scanner, monkeypatch):
        monkeypatch.setenv("FACEBOOK_TOKEN", "fake-fb-token")

        def fake_graph(endpoint, token, params=None):
            if endpoint == "/me/notifications":
                return {"data": [SAMPLE_NOTIFICATION]}
            if endpoint == "/me/conversations":
                return {"data": [SAMPLE_CONVERSATION]}
            return None

        with patch.object(scanner, "_graph", side_effect=fake_graph):
            pollen, wm = scanner.poll(
                {"token_env": "FACEBOOK_TOKEN", "max_items": 20},
                "2026-03-14T00:00:00Z",
            )

        assert len(pollen) >= 1
        for p in pollen:
            assert set(p.keys()) >= REQUIRED_POLLEN_KEYS

    def test_api_error_preserves_watermark(self, scanner, monkeypatch):
        monkeypatch.setenv("FACEBOOK_TOKEN", "fake-fb-token")

        def fake_graph(endpoint, token, params=None):
            return None  # Simulate API errors for all calls

        with patch.object(scanner, "_graph", side_effect=fake_graph):
            pollen, wm = scanner.poll(
                {"token_env": "FACEBOOK_TOKEN", "max_items": 20},
                "2026-03-15T09:00:00Z",
            )

        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"
