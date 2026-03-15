"""Tests for Discord scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch

import pytest

_spec = importlib.util.spec_from_file_location(
    "discord_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "discord", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
DiscordScanner = _mod.DiscordScanner

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}

SAMPLE_DM_CHANNEL = {"id": "dm-chan-1", "type": 1}

SAMPLE_DM_MESSAGE = {
    "id": "900000000000000001",
    "content": "Hey, got a sec?",
    "author": {"id": "user-alice", "username": "alice"},
    "mentions": [],
    "guild_id": "",
}

SAMPLE_MENTION_MESSAGE = {
    "id": "900000000000000002",
    "content": "Hey <@USER123> check this",
    "author": {"id": "user-bob", "username": "bob"},
    "mentions": [{"id": "USER123"}],
    "guild_id": "guild-1",
}

SAMPLE_INLINE_MENTION_MESSAGE = {
    "id": "900000000000000003",
    "content": "FYI <@USER123> this is relevant",
    "author": {"id": "user-carol", "username": "carol"},
    "mentions": [],
    "guild_id": "guild-1",
}

SAMPLE_PUBLIC_UNMATCHED_MESSAGE = {
    "id": "900000000000000004",
    "content": "Just chatting about nothing",
    "author": {"id": "user-dave", "username": "dave"},
    "mentions": [],
    "guild_id": "guild-1",
}


@pytest.fixture
def scanner():
    return DiscordScanner()


class TestDiscordScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["token_env"] == "DISCORD_BOT_TOKEN"
        assert config["watch_channels"] == []
        assert config["watch_dms"] is True
        assert config["user_id"] == ""
        assert config["max_messages"] == 20

    def test_poll_empty_when_no_token(self, scanner, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        pollen, wm = scanner.poll(
            {"token_env": "DISCORD_BOT_TOKEN", "watch_channels": [], "watch_dms": True, "user_id": "", "max_messages": 20},
            "0",
        )
        assert pollen == []
        assert wm == "0"

    def test_dm_emits_discord_dm(self, scanner, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-bot-token")

        def fake_api(endpoint, token, params=None):
            if endpoint == "/users/@me/channels":
                return [SAMPLE_DM_CHANNEL]
            if endpoint.startswith("/channels/dm-chan-1/messages"):
                return [SAMPLE_DM_MESSAGE]
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "DISCORD_BOT_TOKEN", "watch_channels": [], "watch_dms": True, "user_id": "", "max_messages": 20},
                "",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "discord_dm"
        assert pollen[0]["source"] == "discord"
        assert pollen[0]["author"] == "user-alice"
        assert pollen[0]["author_name"] == "alice"
        assert pollen[0]["group"] == "DMs"

    def test_mention_in_mentions_array_emits_discord_mention(self, scanner, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-bot-token")

        def fake_api(endpoint, token, params=None):
            if endpoint == "/users/@me/channels":
                return []
            if endpoint.startswith("/channels/chan-1/messages"):
                return [SAMPLE_MENTION_MESSAGE]
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "DISCORD_BOT_TOKEN", "watch_channels": ["chan-1"], "watch_dms": False, "user_id": "USER123", "max_messages": 20},
                "",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "discord_mention"
        assert pollen[0]["source"] == "discord"
        assert pollen[0]["author_name"] == "bob"

    def test_inline_mention_emits_discord_mention(self, scanner, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-bot-token")

        def fake_api(endpoint, token, params=None):
            if endpoint == "/users/@me/channels":
                return []
            if endpoint.startswith("/channels/chan-1/messages"):
                return [SAMPLE_INLINE_MENTION_MESSAGE]
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "DISCORD_BOT_TOKEN", "watch_channels": ["chan-1"], "watch_dms": False, "user_id": "USER123", "max_messages": 20},
                "",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "discord_mention"
        assert pollen[0]["author_name"] == "carol"

    def test_non_matching_message_skipped(self, scanner, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-bot-token")

        def fake_api(endpoint, token, params=None):
            if endpoint == "/users/@me/channels":
                return []
            if endpoint.startswith("/channels/chan-1/messages"):
                return [SAMPLE_PUBLIC_UNMATCHED_MESSAGE]
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "DISCORD_BOT_TOKEN", "watch_channels": ["chan-1"], "watch_dms": False, "user_id": "USER123", "max_messages": 20},
                "",
            )

        assert pollen == []

    def test_watermark_advances_to_highest_snowflake(self, scanner, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-bot-token")

        messages = [
            {
                "id": "900000000000000010",
                "content": "First DM",
                "author": {"id": "user-alice", "username": "alice"},
                "mentions": [],
                "guild_id": "",
            },
            {
                "id": "900000000000000050",
                "content": "Second DM",
                "author": {"id": "user-bob", "username": "bob"},
                "mentions": [],
                "guild_id": "",
            },
            {
                "id": "900000000000000030",
                "content": "Third DM",
                "author": {"id": "user-carol", "username": "carol"},
                "mentions": [],
                "guild_id": "",
            },
        ]

        def fake_api(endpoint, token, params=None):
            if endpoint == "/users/@me/channels":
                return [SAMPLE_DM_CHANNEL]
            if endpoint.startswith("/channels/dm-chan-1/messages"):
                return messages
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "DISCORD_BOT_TOKEN", "watch_channels": [], "watch_dms": True, "user_id": "", "max_messages": 20},
                "",
            )

        assert len(pollen) == 3
        assert wm == "900000000000000050"

    def test_pollen_schema_has_all_required_keys(self, scanner, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-bot-token")

        def fake_api(endpoint, token, params=None):
            if endpoint == "/users/@me/channels":
                return [SAMPLE_DM_CHANNEL]
            if endpoint.startswith("/channels/dm-chan-1/messages"):
                return [SAMPLE_DM_MESSAGE]
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "DISCORD_BOT_TOKEN", "watch_channels": [], "watch_dms": True, "user_id": "", "max_messages": 20},
                "",
            )

        assert len(pollen) == 1
        assert set(pollen[0].keys()) >= REQUIRED_POLLEN_KEYS

    def test_api_error_preserves_watermark(self, scanner, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-bot-token")

        def fake_api(endpoint, token, params=None):
            return None  # All API calls fail

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {"token_env": "DISCORD_BOT_TOKEN", "watch_channels": ["chan-1"], "watch_dms": True, "user_id": "", "max_messages": 20},
                "800000000000000000",
            )

        assert pollen == []
        assert wm == "800000000000000000"
