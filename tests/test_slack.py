"""Tests for Slack scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_spec = importlib.util.spec_from_file_location(
    "slack_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "slack", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SlackScanner = _mod.SlackScanner

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}

SAMPLE_DM_CHANNEL = {
    "id": "D001",
    "is_im": True,
}

SAMPLE_DM_MESSAGE = {
    "ts": "1710500000.000100",
    "text": "Hey, can you check this?",
    "user": "U999",
    "user_profile": {"real_name": "Alice"},
}

SAMPLE_MENTION_MESSAGE = {
    "ts": "1710500100.000200",
    "text": "Hey <@U123> please review this PR",
    "user": "U888",
    "user_profile": {"real_name": "Bob"},
}

SAMPLE_THREAD_REPLY_MESSAGE = {
    "ts": "1710500200.000300",
    "thread_ts": "1710500000.000100",
    "text": "Replying in thread",
    "user": "U777",
}

SAMPLE_PUBLIC_UNMATCHED_MESSAGE = {
    "ts": "1710500300.000400",
    "text": "Just a normal message in a channel",
    "user": "U666",
}


@pytest.fixture
def scanner():
    return SlackScanner()


class TestSlackScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["token_env"] == "SLACK_TOKEN"
        assert config["watch_channels"] == []
        assert config["watch_dms"] is True
        assert config["username"] == ""
        assert config["max_messages"] == 20

    def test_poll_empty_when_no_token(self, scanner, monkeypatch):
        monkeypatch.delenv("SLACK_TOKEN", raising=False)
        pollen, wm = scanner.poll(
            {"token_env": "SLACK_TOKEN", "watch_channels": [], "watch_dms": True},
            "2026-03-15T09:00:00Z",
        )
        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"

    def test_dm_message_emits_slack_dm(self, scanner, monkeypatch):
        monkeypatch.setenv("SLACK_TOKEN", "xoxb-fake-token")

        def fake_api(method, token, params=None):
            if method == "conversations.list":
                return {"ok": True, "channels": [SAMPLE_DM_CHANNEL]}
            if method == "conversations.history":
                return {"ok": True, "messages": [SAMPLE_DM_MESSAGE]}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "SLACK_TOKEN", "watch_channels": [], "watch_dms": True, "username": "U123", "max_messages": 20},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "slack_dm"
        assert pollen[0]["source"] == "slack"
        assert pollen[0]["author"] == "U999"
        assert pollen[0]["author_name"] == "Alice"
        assert pollen[0]["group"] == "DMs"

    def test_mention_emits_slack_mention(self, scanner, monkeypatch):
        monkeypatch.setenv("SLACK_TOKEN", "xoxb-fake-token")

        def fake_api(method, token, params=None):
            if method == "conversations.history":
                return {"ok": True, "messages": [SAMPLE_MENTION_MESSAGE]}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "SLACK_TOKEN", "watch_channels": ["C001"], "watch_dms": False, "username": "U123", "max_messages": 20},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "slack_mention"
        assert "<@U123>" in SAMPLE_MENTION_MESSAGE["text"]

    def test_thread_reply_emits_slack_thread_reply(self, scanner, monkeypatch):
        monkeypatch.setenv("SLACK_TOKEN", "xoxb-fake-token")

        def fake_api(method, token, params=None):
            if method == "conversations.history":
                return {"ok": True, "messages": [SAMPLE_THREAD_REPLY_MESSAGE]}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "SLACK_TOKEN", "watch_channels": ["C001"], "watch_dms": False, "username": "U123", "max_messages": 20},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "slack_thread_reply"

    def test_non_matching_message_skipped(self, scanner, monkeypatch):
        monkeypatch.setenv("SLACK_TOKEN", "xoxb-fake-token")

        def fake_api(method, token, params=None):
            if method == "conversations.history":
                return {"ok": True, "messages": [SAMPLE_PUBLIC_UNMATCHED_MESSAGE]}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "SLACK_TOKEN", "watch_channels": ["C001"], "watch_dms": False, "username": "U123", "max_messages": 20},
                "2026-03-15T09:00:00Z",
            )

        assert pollen == []

    def test_pollen_schema_has_all_required_keys(self, scanner, monkeypatch):
        monkeypatch.setenv("SLACK_TOKEN", "xoxb-fake-token")

        def fake_api(method, token, params=None):
            if method == "conversations.list":
                return {"ok": True, "channels": [SAMPLE_DM_CHANNEL]}
            if method == "conversations.history":
                return {"ok": True, "messages": [SAMPLE_DM_MESSAGE]}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "SLACK_TOKEN", "watch_channels": [], "watch_dms": True, "username": "", "max_messages": 20},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert set(pollen[0].keys()) >= REQUIRED_POLLEN_KEYS

    def test_api_error_preserves_watermark(self, scanner, monkeypatch):
        monkeypatch.setenv("SLACK_TOKEN", "xoxb-fake-token")

        def fake_api(method, token, params=None):
            if method == "conversations.list":
                return {"ok": False, "error": "invalid_auth"}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "SLACK_TOKEN", "watch_channels": [], "watch_dms": True, "username": "", "max_messages": 20},
                "2026-03-15T09:00:00Z",
            )

        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"
