"""Tests for Telegram scanner."""

import importlib.util
import json
import os
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
        "message_id": 1,
        "date": 1_767_225_600,
        "chat": {"id": 12345, "type": "group", "title": "Dev Team"},
        "from": {
            "id": 101,
            "is_bot": False,
            "username": "alice",
            "first_name": "Alice",
        },
        "text": "Hello everyone",
    },
}

SAMPLE_MENTION_UPDATE = {
    "update_id": 100002,
    "message": {
        "message_id": 2,
        "date": 1_767_225_601,
        "chat": {"id": 12345, "type": "group", "title": "Dev Team"},
        "from": {
            "id": 102,
            "is_bot": False,
            "username": "bob",
            "first_name": "Bob",
        },
        "text": "Hey @mybot check this",
    },
}

SAMPLE_REPLY_UPDATE = {
    "update_id": 100003,
    "message": {
        "message_id": 3,
        "date": 1_767_225_602,
        "chat": {"id": 12345, "type": "group", "title": "Dev Team"},
        "from": {
            "id": 103,
            "is_bot": False,
            "username": "carol",
            "first_name": "Carol",
        },
        "text": "Sure, will do",
        "reply_to_message": {
            "message_id": 999,
            "text": "Original message",
            "from": {
                "id": 999,
                "is_bot": True,
                "first_name": "Hive",
                "username": "mybot",
            },
        },
    },
}

GET_ME_RESPONSE = {
    "ok": True,
    "result": {
        "id": 999,
        "is_bot": True,
        "first_name": "Hive",
        "username": "mybot",
    },
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
                "message_id": 4,
                "date": 1_767_225_603,
                "chat": {"id": 99999, "type": "group", "title": "Other Chat"},
                "from": {
                    "id": 104,
                    "is_bot": False,
                    "username": "dave",
                    "first_name": "Dave",
                },
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
        state = json.loads(wm)
        assert state["last_update_id"] == 100002
        assert state["initialized"] is True

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

    def test_first_install_snapshots_newest_update_with_negative_offset(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        newest = {**SAMPLE_UPDATE, "update_id": 200001}

        with patch.object(
            scanner,
            "_api",
            return_value={"ok": True, "result": [newest]},
        ) as api:
            pollen, watermark = scanner.poll(
                {**scanner.configure(), "max_messages": 2},
                "",
            )
        assert pollen == []
        state = json.loads(watermark)
        assert state["initialized"] is True
        assert state["last_update_id"] == 200001
        params = api.call_args.args[2]
        assert params["offset"] == "-1"
        assert params["limit"] == "1"
        assert "edited_channel_post" in json.loads(params["allowed_updates"])

    def test_unsupported_updates_are_acknowledged(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        update = {"update_id": 300001, "callback_query": {"id": "callback"}}
        with patch.object(scanner, "_api", return_value={"ok": True, "result": [update]}):
            pollen, watermark = scanner.poll(scanner.configure(), "0")
        assert pollen == []
        assert json.loads(watermark)["last_update_id"] == 300001

    def test_invalid_chat_id_fails_before_api(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        with patch.object(scanner, "_api") as api:
            pollen, watermark = scanner.poll(
                {**scanner.configure(), "watch_chats": ["../../chat"]},
                "safe",
            )
        assert (pollen, watermark) == ([], "safe")
        api.assert_not_called()

    def test_get_me_failure_preserves_update_for_retry(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")

        def fake_api(method, token, params=None):
            if method == "getUpdates":
                return {"ok": True, "result": [SAMPLE_UPDATE]}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            assert scanner.poll(scanner.configure(), "0") == ([], "0")

    def test_edited_and_channel_post_updates_are_processed(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        edited = {
            "update_id": 400001,
            "edited_message": SAMPLE_UPDATE["message"],
        }
        channel = {
            "update_id": 400002,
            "channel_post": {
                "message_id": 5,
                "date": 1_767_225_604,
                "chat": {
                    "id": -100123,
                    "type": "channel",
                    "title": "Announcements",
                },
                "sender_chat": {
                    "id": -100123,
                    "type": "channel",
                    "title": "Announcements",
                },
                "text": "Release shipped",
            },
        }

        def fake_api(method, token, params=None):
            if method == "getUpdates":
                return {"ok": True, "result": [edited, channel]}
            return GET_ME_RESPONSE

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, watermark = scanner.poll(scanner.configure(), "0")

        assert [item["metadata"]["update_type"] for item in pollen] == [
            "edited_message",
            "channel_post",
        ]
        assert pollen[1]["author_name"] == "Announcements"
        assert json.loads(watermark)["last_update_id"] == 400002

    def test_malformed_update_does_not_advance_past_it(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        malformed = {"update_id": "400003", "message": SAMPLE_UPDATE["message"]}
        with patch.object(
            scanner, "_api", return_value={"ok": True, "result": [malformed]}
        ):
            assert scanner.poll(scanner.configure(), "0") == ([], "0")

    def test_corrupt_current_state_and_invalid_config_fail_before_api(
        self, scanner, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        corrupt_states = [
            json.dumps({
                "version": 2,
                "last_update_id": 10,
                "initialized": "true",
            }),
            json.dumps({
                "version": 2,
                "last_update_id": 10,
                "initialized": True,
                "unexpected": True,
            }),
            '{"version":2,"version":2,"last_update_id":10,"initialized":true}',
        ]
        with patch.object(scanner, "_api") as api:
            for state in corrupt_states:
                assert scanner.poll(scanner.configure(), state) == ([], state)
        api.assert_not_called()

        invalid_configs = [
            {**scanner.configure(), "watch_chats": 12345},
            {**scanner.configure(), "watch_chats": [12345, "12345"]},
            {**scanner.configure(), "token_env": 123},
            {**scanner.configure(), "bot_username": ["mybot"]},
            {**scanner.configure(), "max_messages": True},
        ]
        with patch.object(scanner, "_api") as api:
            for config in invalid_configs:
                assert scanner.poll(config, "0") == ([], "0")
        api.assert_not_called()

    def test_malformed_late_message_and_nonascending_batch_are_transactional(
        self, scanner, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        malformed = json.loads(json.dumps(SAMPLE_MENTION_UPDATE))
        malformed["update_id"] = 100004
        malformed["message"]["from"]["username"] = {"bad": "shape"}
        batches = [
            [SAMPLE_UPDATE, malformed],
            [SAMPLE_MENTION_UPDATE, SAMPLE_UPDATE],
            [SAMPLE_UPDATE, SAMPLE_UPDATE],
        ]
        for batch in batches:
            with patch.object(
                scanner, "_api", return_value={"ok": True, "result": batch}
            ):
                assert scanner.poll(scanner.configure(), "0") == ([], "0")

    def test_random_update_id_rollover_after_quiet_week_is_accepted(
        self, scanner, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        state = json.dumps({
            "version": 2,
            "last_update_id": 9_000_000,
            "initialized": True,
        })
        rolled_over = {**SAMPLE_UPDATE, "update_id": 42}

        def fake_api(method, token, params=None):
            if method == "getUpdates":
                assert "offset" not in params
                return {"ok": True, "result": [rolled_over]}
            return GET_ME_RESPONSE

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, watermark = scanner.poll(scanner.configure(), state)
        assert [item["id"] for item in pollen] == ["telegram-42"]
        next_state = json.loads(watermark)
        assert next_state["version"] == 3
        assert next_state["last_update_id"] == 42
        assert next_state["last_update_seen_at"]

    def test_recent_update_state_uses_normal_sequential_offset(
        self, scanner, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        state = json.dumps({
            "version": 3,
            "last_update_id": 100001,
            "initialized": True,
            "last_update_seen_at": scanner._utc_now_z(),
        })
        with patch.object(
            scanner, "_api", return_value={"ok": True, "result": []}
        ) as api:
            pollen, watermark = scanner.poll(scanner.configure(), state)
        assert pollen == []
        assert json.loads(watermark) == json.loads(state)
        assert api.call_args.args[2]["offset"] == "100002"

    def test_current_business_and_guest_message_updates_are_supported(
        self, scanner, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        business = {
            "update_id": 500001,
            "business_message": SAMPLE_UPDATE["message"],
        }
        guest = {
            "update_id": 500002,
            "guest_message": SAMPLE_MENTION_UPDATE["message"],
        }

        def fake_api(method, token, params=None):
            if method == "getUpdates":
                allowed = json.loads(params["allowed_updates"])
                assert "business_message" in allowed
                assert "guest_message" in allowed
                return {"ok": True, "result": [business, guest]}
            return GET_ME_RESPONSE

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, _ = scanner.poll(scanner.configure(), "0")
        assert [item["metadata"]["update_type"] for item in pollen] == [
            "business_message",
            "guest_message",
        ]

    def test_unwatched_batch_acks_without_identity_request(self, scanner, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        methods = []

        def fake_api(method, token, params=None):
            methods.append(method)
            return {"ok": True, "result": [SAMPLE_UPDATE]}

        config = {**scanner.configure(), "watch_chats": [99999]}
        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, watermark = scanner.poll(config, "0")
        assert pollen == []
        assert methods == ["getUpdates"]
        assert json.loads(watermark)["last_update_id"] == 100001

    def test_bot_identity_mismatch_and_self_messages_fail_or_suppress(
        self, scanner, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        with patch.object(
            scanner,
            "_api",
            side_effect=[{"ok": True, "result": [SAMPLE_UPDATE]}, GET_ME_RESPONSE],
        ):
            config = {**scanner.configure(), "bot_username": "wrongbot"}
            assert scanner.poll(config, "0") == ([], "0")

        own_update = json.loads(json.dumps(SAMPLE_UPDATE))
        own_update["message"]["from"] = GET_ME_RESPONSE["result"]
        config = {
            **scanner.configure(),
            "bot_username": "mybot",
            "bot_user_id": "999",
        }
        with patch.object(
            scanner,
            "_api",
            return_value={"ok": True, "result": [own_update]},
        ) as api:
            pollen, watermark = scanner.poll(config, "0")
        assert pollen == []
        assert api.call_count == 1
        assert json.loads(watermark)["last_update_id"] == 100001

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers(self):
        with pytest.raises(ValueError):
            _mod._strict_json('{"ok":true,"ok":false}')
        with pytest.raises(ValueError):
            _mod._strict_json('{"ok":true,"result":NaN}')
