"""Functional tests for Discord's explicit-channel REST scanner."""

import importlib.util
import json
import os
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "discord_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "discord", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
DiscordScanner = _mod.DiscordScanner

CHANNEL = "111111111111111111"
DM_CHANNEL = "222222222222222222"
USER = "333333333333333333"


def _message(msg_id, *, content="hello", author=USER, guild_id="444444444444444444", mentions=None, bot=False):
    return {
        "id": str(msg_id),
        "content": content,
        "author": {"id": author, "username": "alice", "bot": bot},
        "mentions": mentions or [],
        "guild_id": guild_id,
        "attachments": [],
    }


def _state(channel, after="800000000000000000"):
    return json.dumps({
        "version": 2,
        "channels": {channel: {"initialized": True, "after": after}},
        "next_index": 0,
    })


def _config(scanner, **changes):
    return {**scanner.configure(), **changes}


def test_defaults_require_explicit_dm_channels():
    config = DiscordScanner().configure()
    assert config["token_env"] == "DISCORD_BOT_TOKEN"
    assert config["watch_channels"] == []
    assert config["watch_dm_channels"] == []
    assert config["watch_dms"] is False
    assert config["max_messages"] == 100
    assert config["channels_per_poll"] == 10


def test_missing_token_or_impossible_dm_discovery_preserves_state(monkeypatch):
    scanner = DiscordScanner()
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert scanner.poll(scanner.configure(), "safe") == ([], "safe")

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    config = _config(scanner, watch_dms=True, watch_dm_channels=[])
    assert scanner.poll(config, "safe") == ([], "safe")


def test_explicit_dm_channel_emits_dm(monkeypatch):
    scanner = DiscordScanner()
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    config = _config(
        scanner,
        watch_dms=True,
        watch_dm_channels=[DM_CHANNEL],
    )
    message = _message("900000000000000001", guild_id="")
    with patch.object(scanner, "_api", return_value=[message]) as api:
        pollen, watermark = scanner.poll(config, _state(DM_CHANNEL))
    assert [item["type"] for item in pollen] == ["discord_dm"]
    assert pollen[0]["url"].startswith("https://discord.com/channels/@me/")
    assert pollen[0]["metadata"]["channel_id"] == DM_CHANNEL
    assert json.loads(watermark)["channels"][DM_CHANNEL]["after"] == message["id"]
    assert "/users/@me/channels" not in api.call_args.args[0]


def test_guild_channel_emits_only_exact_user_mentions(monkeypatch):
    scanner = DiscordScanner()
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    config = _config(scanner, watch_channels=[CHANNEL], user_id=USER)
    messages = [
        _message("900000000000000003", content=f"hi <@!{USER}>", mentions=[]),
        _message("900000000000000002", content="array mention", mentions=[{"id": USER}]),
        _message("900000000000000001", content="ordinary chatter"),
    ]
    with patch.object(scanner, "_api", return_value=messages):
        pollen, _ = scanner.poll(config, _state(CHANNEL))
    assert [item["type"] for item in pollen] == [
        "discord_mention",
        "discord_mention",
    ]
    assert all(item["url"].startswith("https://discord.com/channels/444") for item in pollen)


def test_bootstrap_silences_history_and_records_highest_snowflake(monkeypatch):
    scanner = DiscordScanner()
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    config = _config(scanner, watch_dms=True, watch_dm_channels=[DM_CHANNEL])
    messages = [_message("900000000000000050"), _message("900000000000000010")]
    with patch.object(scanner, "_api", return_value=messages):
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    channel_state = json.loads(watermark)["channels"][DM_CHANNEL]
    assert channel_state == {"initialized": True, "after": "900000000000000050"}


def test_multi_page_burst_retains_cursor_until_committed_boundary(monkeypatch):
    scanner = DiscordScanner()
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    config = _config(
        scanner,
        watch_dms=True,
        watch_dm_channels=[DM_CHANNEL],
        max_messages=2,
    )
    watermark = _state(DM_CHANNEL, after="100")
    for expected_ids, response_ids in [
        (["discord-105", "discord-104"], [105, 104]),
        (["discord-103", "discord-102"], [103, 102]),
        (["discord-101"], [101, 100]),
    ]:
        with patch.object(
            scanner,
            "_api",
            return_value=[_message(value, guild_id="") for value in response_ids],
        ):
            pollen, watermark = scanner.poll(config, watermark)
        assert [item["id"] for item in pollen] == expected_ids
    assert json.loads(watermark)["channels"][DM_CHANNEL]["after"] == "105"


def test_channel_rotation_prevents_starvation(monkeypatch):
    scanner = DiscordScanner()
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    other = "555555555555555555"
    config = _config(
        scanner,
        watch_dms=True,
        watch_dm_channels=[DM_CHANNEL, other],
        channels_per_poll=1,
    )
    endpoints = []

    def fake(endpoint, token, params=None):
        endpoints.append(endpoint)
        return []

    with patch.object(scanner, "_api", side_effect=fake):
        _, watermark = scanner.poll(config, "")
        scanner.poll(config, watermark)
    assert DM_CHANNEL in endpoints[0]
    assert other in endpoints[1]


def test_api_error_and_invalid_snowflakes_fail_closed(monkeypatch):
    scanner = DiscordScanner()
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    invalid = _config(scanner, watch_channels=["../../escape"])
    assert scanner.poll(invalid, "safe") == ([], "safe")

    bad_user = _config(scanner, watch_channels=[CHANNEL], user_id="not-numeric")
    assert scanner.poll(bad_user, "safe") == ([], "safe")

    valid = _config(scanner, watch_channels=[CHANNEL], user_id=USER)
    with patch.object(scanner, "_api", return_value=None):
        assert scanner.poll(valid, "safe") == ([], "safe")


def test_failed_channel_rotates_and_successful_channel_commits(monkeypatch):
    scanner = DiscordScanner()
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    other = "555555555555555555"
    config = _config(
        scanner,
        watch_channels=[CHANNEL, other],
        user_id=USER,
        channels_per_poll=2,
    )
    state = json.dumps({
        "version": 2,
        "channels": {
            CHANNEL: {"initialized": True, "after": "800000000000000000"},
            other: {"initialized": True, "after": "800000000000000000"},
        },
        "next_index": 0,
    })
    responses = [None, [_message("900000000000000001", content=f"hi <@{USER}>")]]

    with patch.object(scanner, "_api", side_effect=responses):
        pollen, watermark = scanner.poll(config, state)

    updated = json.loads(watermark)
    assert [item["metadata"]["channel_id"] for item in pollen] == [other]
    assert updated["channels"][CHANNEL]["after"] == "800000000000000000"
    assert updated["channels"][other]["after"] == "900000000000000001"


def test_malformed_message_page_does_not_advance_that_channel(monkeypatch):
    scanner = DiscordScanner()
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    config = _config(scanner, watch_channels=[CHANNEL], user_id=USER)
    state = _state(CHANNEL)
    with patch.object(scanner, "_api", return_value=[{"content": "missing id"}]):
        assert scanner.poll(config, state) == ([], state)


def test_malformed_late_message_and_order_do_not_commit_page(monkeypatch):
    scanner = DiscordScanner()
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    config = _config(scanner, watch_channels=[CHANNEL], user_id=USER)
    state = _state(CHANNEL)
    malformed = _message("900000000000000001")
    malformed["author"] = {"id": USER, "username": {"bad": "shape"}}
    for messages in (
        [_message("900000000000000003"), malformed],
        [_message("900000000000000001"), _message("900000000000000002")],
    ):
        with patch.object(scanner, "_api", return_value=messages):
            assert scanner.poll(config, state) == ([], state)


def test_invalid_current_state_and_config_collections_fail_closed(monkeypatch):
    scanner = DiscordScanner()
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    config = _config(scanner, watch_channels=[CHANNEL], user_id=USER)
    corrupt = json.loads(_state(CHANNEL))
    corrupt["channels"][CHANNEL]["initialized"] = "true"
    corrupt_state = json.dumps(corrupt)
    with patch.object(scanner, "_api") as api:
        assert scanner.poll(config, corrupt_state) == ([], corrupt_state)
    api.assert_not_called()

    for invalid in (
        _config(scanner, watch_channels=CHANNEL),
        _config(scanner, watch_channels=[CHANNEL], user_id={"id": USER}),
        _config(scanner, watch_channels=[CHANNEL], token_env=123),
    ):
        with patch.object(scanner, "_api") as api:
            assert scanner.poll(invalid, "safe") == ([], "safe")
        api.assert_not_called()
