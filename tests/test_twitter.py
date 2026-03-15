"""Tests for Twitter/X scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_TWITTER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "community", "twitter", "adapter.py"
)
_spec = importlib.util.spec_from_file_location("twitter_adapter", _TWITTER_PATH)
_twitter_mod = importlib.util.module_from_spec(_spec)
sys.modules["twitter_adapter"] = _twitter_mod
_spec.loader.exec_module(_twitter_mod)
TwitterScanner = _twitter_mod.TwitterScanner


SAMPLE_MENTIONS_RESPONSE = {
    "data": [
        {
            "id": "tweet001",
            "author_id": "author123",
            "text": "Hey @dhruvil check this out",
            "created_at": "2026-03-15T10:00:00Z",
        },
    ],
    "includes": {
        "users": [
            {
                "id": "author123",
                "username": "alice",
                "name": "Alice Smith",
            },
        ],
    },
}

SAMPLE_DM_RESPONSE = {
    "data": [
        {
            "id": "dm001",
            "sender_id": "sender456",
            "text": "Can we chat about the project?",
            "created_at": "2026-03-15T11:00:00Z",
        },
    ],
}

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}


@pytest.fixture
def scanner():
    return TwitterScanner()


class TestTwitterScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["token_env"] == "TWITTER_BEARER_TOKEN"
        assert config["username"] == ""
        assert config["user_id"] == ""
        assert config["watch_dms"] is True
        assert config["max_items"] == 20

    def test_poll_empty_when_no_token(self, scanner):
        with patch.dict(os.environ, {}, clear=True):
            pollen, wm = scanner.poll(
                {"token_env": "TWITTER_BEARER_TOKEN", "user_id": "uid1"},
                "2026-03-15T09:00:00Z",
            )
        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"

    def test_poll_empty_when_no_user_id(self, scanner):
        with patch.dict(os.environ, {"TWITTER_BEARER_TOKEN": "tok123"}):
            pollen, wm = scanner.poll(
                {"token_env": "TWITTER_BEARER_TOKEN", "user_id": ""},
                "2026-03-15T09:00:00Z",
            )
        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"

    def test_mention_emits_twitter_mention(self, scanner):
        with patch.dict(os.environ, {"TWITTER_BEARER_TOKEN": "tok123"}), \
             patch.object(scanner, "_api") as mock_api:
            mock_api.side_effect = [
                SAMPLE_MENTIONS_RESPONSE,  # mentions call
                {"data": []},              # dm_events call
            ]
            pollen, wm = scanner.poll(
                {"token_env": "TWITTER_BEARER_TOKEN", "user_id": "uid1",
                 "max_items": 20, "watch_dms": True},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "twitter_mention"
        assert p["id"] == "twitter-mention-tweet001"
        assert p["source"] == "twitter"
        assert p["author"] == "alice"
        assert p["author_name"] == "Alice Smith"
        assert p["group"] == "Mentions"
        assert "tweet001" in p["url"]

    def test_dm_emits_twitter_dm(self, scanner):
        with patch.dict(os.environ, {"TWITTER_BEARER_TOKEN": "tok123"}), \
             patch.object(scanner, "_api") as mock_api:
            mock_api.side_effect = [
                {"data": []},        # mentions call
                SAMPLE_DM_RESPONSE,  # dm_events call
            ]
            pollen, wm = scanner.poll(
                {"token_env": "TWITTER_BEARER_TOKEN", "user_id": "uid1",
                 "max_items": 20, "watch_dms": True},
                "",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "twitter_dm"
        assert p["id"] == "twitter-dm-dm001"
        assert p["source"] == "twitter"
        assert p["author"] == "sender456"
        assert p["group"] == "DMs"

    def test_dm_watermark_filters_old(self, scanner):
        old_dm = {
            "data": [
                {
                    "id": "dm_old",
                    "sender_id": "sender456",
                    "text": "Old message",
                    "created_at": "2026-03-15T08:00:00Z",
                },
                {
                    "id": "dm_new",
                    "sender_id": "sender789",
                    "text": "New message",
                    "created_at": "2026-03-15T12:00:00Z",
                },
            ],
        }
        with patch.dict(os.environ, {"TWITTER_BEARER_TOKEN": "tok123"}), \
             patch.object(scanner, "_api") as mock_api:
            mock_api.side_effect = [
                {"data": []},  # mentions
                old_dm,        # dm_events
            ]
            pollen, wm = scanner.poll(
                {"token_env": "TWITTER_BEARER_TOKEN", "user_id": "uid1",
                 "max_items": 20, "watch_dms": True},
                "2026-03-15T09:00:00Z",
            )

        # Only the new DM should make it through
        assert len(pollen) == 1
        assert pollen[0]["id"] == "twitter-dm-dm_new"

    def test_pollen_schema_has_all_required_keys(self, scanner):
        with patch.dict(os.environ, {"TWITTER_BEARER_TOKEN": "tok123"}), \
             patch.object(scanner, "_api") as mock_api:
            mock_api.side_effect = [
                SAMPLE_MENTIONS_RESPONSE,
                SAMPLE_DM_RESPONSE,
            ]
            pollen, wm = scanner.poll(
                {"token_env": "TWITTER_BEARER_TOKEN", "user_id": "uid1",
                 "max_items": 20, "watch_dms": True},
                "",
            )

        assert len(pollen) == 2
        for p in pollen:
            missing = REQUIRED_POLLEN_KEYS - set(p.keys())
            assert not missing, f"Pollen missing keys: {missing}"

    def test_api_error_preserves_watermark(self, scanner):
        original_wm = "2026-03-15T09:00:00Z"
        with patch.dict(os.environ, {"TWITTER_BEARER_TOKEN": "tok123"}), \
             patch.object(scanner, "_api") as mock_api:
            # Both calls return None (API error)
            mock_api.side_effect = [None, None]
            pollen, wm = scanner.poll(
                {"token_env": "TWITTER_BEARER_TOKEN", "user_id": "uid1",
                 "max_items": 20, "watch_dms": True},
                original_wm,
            )

        assert wm == original_wm
