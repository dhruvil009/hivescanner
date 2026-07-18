"""Functional tests for Slack per-channel cursors and rate-tier safety."""

import importlib.util
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest


_spec = importlib.util.spec_from_file_location(
    "slack_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "slack", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SlackScanner = _mod.SlackScanner

CHANNEL = "C001"
DM = "D001"
USER = "U123"


def _message(ts, text="hello", user="U999", **extra):
    return {
        "type": "message", "ts": ts, "text": text, "user": user,
        "user_profile": {"real_name": "Alice"}, **extra,
    }


def _state(channels=None, *, dms=None, next_index=0):
    return json.dumps({
        "version": 2, "channels": channels or {}, "next_index": next_index,
        "dm_channels": dms or [],
        "dm_refreshed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "authenticated_user_id": USER,
    }, sort_keys=True, separators=(",", ":"))


def _config(scanner, **changes):
    return {**scanner.configure(), "user_id": USER, **changes}


def test_defaults_match_restricted_history_rate_tier():
    config = SlackScanner().configure()
    assert config["watch_dms"] is True
    assert config["max_messages"] == 15
    assert config["history_requests_per_poll"] == 1
    assert config["allow_high_tier_rate_limits"] is False
    assert config["dm_discovery_max_pages"] == 10


def test_missing_or_invalid_token_env_preserves_watermark(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.delenv("SLACK_TOKEN", raising=False)
    assert scanner.poll(scanner.configure(), "safe") == ([], "safe")
    assert scanner.poll({"token_env": "BAD-NAME"}, "safe") == ([], "safe")


def test_dm_message_uses_cached_discovered_channel(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-token")
    state = _state({DM: {"initialized": True, "oldest": "1710500000.000000"}}, dms=[DM])
    config = _config(scanner)
    message = _message("1710500001.000100", "Can you check this?")
    with patch.object(scanner, "_api", return_value={"ok": True, "messages": [message]}):
        pollen, watermark = scanner.poll(config, state)
    assert [item["type"] for item in pollen] == ["slack_dm"]
    assert pollen[0]["id"] == f"slack-{DM}-1710500001.000100"
    assert pollen[0]["author_name"] == "Alice"
    assert json.loads(watermark)["channels"][DM]["oldest"] == message["ts"]


def test_channel_emits_exact_mentions_and_observable_broadcast_thread_replies(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    state = _state({CHANNEL: {"initialized": True, "oldest": "1710500000.000000"}})
    config = _config(scanner, watch_channels=[CHANNEL], watch_dms=False)
    messages = [
        _message("1710500003.000000", f"hi <@{USER}>", user="U888"),
        _message(
            "1710500002.000000", "broadcast reply", user="U777",
            thread_ts="1710500001.000000",
        ),
        _message("1710500001.000000", "ordinary chatter", user="U666"),
    ]
    with patch.object(scanner, "_api", return_value={"ok": True, "messages": messages}):
        pollen, _ = scanner.poll(config, state)
    assert [item["type"] for item in pollen] == [
        "slack_mention", "slack_thread_reply",
    ]


def test_self_messages_are_suppressed(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    state = _state({DM: {"initialized": True, "oldest": "1.0"}}, dms=[DM])
    with patch.object(scanner, "_api", return_value={
        "ok": True, "messages": [_message("2.0", user=USER)],
    }):
        pollen, _ = scanner.poll(_config(scanner), state)
    assert pollen == []


def test_auth_test_discovers_user_id_when_not_configured_or_cached(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    state = json.dumps({
        "version": 2, "channels": {CHANNEL: {"initialized": True, "oldest": "1.0"}},
        "next_index": 0, "dm_channels": [], "dm_refreshed_at": "",
    })

    def fake(method, token, params=None):
        if method == "auth.test":
            return {"ok": True, "user_id": USER}
        return {"ok": True, "messages": []}

    config = {**scanner.configure(), "watch_channels": [CHANNEL], "watch_dms": False}
    with patch.object(scanner, "_api", side_effect=fake):
        _, watermark = scanner.poll(config, state)
    assert json.loads(watermark)["authenticated_user_id"] == USER


def test_first_channel_poll_is_quiet_and_commits_highest_decimal_timestamp(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    config = _config(scanner, watch_channels=[CHANNEL], watch_dms=False)
    messages = [_message("10.000010"), _message("10.000002")]
    with patch.object(scanner, "_api", return_value={"ok": True, "messages": messages}):
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    assert json.loads(watermark)["channels"][CHANNEL]["oldest"] == "10.000010"


def test_history_cursor_holds_oldest_until_last_page(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    config = _config(scanner, watch_channels=[CHANNEL], watch_dms=False)
    state = _state({CHANNEL: {"initialized": True, "oldest": "1.0"}})
    first = {
        "ok": True, "messages": [_message("3.0", f"<@{USER}> first")],
        "has_more": True, "response_metadata": {"next_cursor": "cursor"},
    }
    with patch.object(scanner, "_api", return_value=first):
        _, continuation = scanner.poll(config, state)
    mid = json.loads(continuation)["channels"][CHANNEL]
    assert mid["oldest"] == "1.0"
    assert mid["cursor"] == "cursor"

    second = {"ok": True, "messages": [_message("2.0", f"<@{USER}> second")]}
    with patch.object(scanner, "_api", return_value=second) as api:
        _, finished = scanner.poll(config, continuation)
    assert api.call_args.args[2]["cursor"] == "cursor"
    assert json.loads(finished)["channels"][CHANNEL]["oldest"] == "3.0"


def test_restricted_tier_forces_one_history_request_and_rotates(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    other = "C002"
    config = _config(
        scanner, watch_channels=[CHANNEL, other], watch_dms=False,
        history_requests_per_poll=50,
    )
    calls = []

    def fake(method, token, params=None):
        calls.append(params["channel"])
        return {"ok": True, "messages": []}

    with patch.object(scanner, "_api", side_effect=fake):
        _, watermark = scanner.poll(config, "")
        scanner.poll(config, watermark)
    assert calls == [CHANNEL, other]


def test_successfully_empty_dm_discovery_is_cached_for_a_day(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    state = _state(dms=[])
    with patch.object(scanner, "_api") as api:
        pollen, _ = scanner.poll(_config(scanner), state)
    assert pollen == []
    api.assert_not_called()


def test_history_error_or_missing_cursor_preserves_watermark(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    config = _config(scanner, watch_channels=[CHANNEL], watch_dms=False)
    state = _state({CHANNEL: {"initialized": True, "oldest": "1.0"}})
    for response in [
        {"ok": False, "error": "ratelimited"},
        {"ok": True, "messages": [], "has_more": True},
    ]:
        with patch.object(scanner, "_api", return_value=response):
            assert scanner.poll(config, state) == ([], state)


def test_failed_channel_rotates_without_rolling_back_successful_channel(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    other = "C002"
    config = _config(
        scanner,
        watch_channels=[CHANNEL, other],
        watch_dms=False,
        history_requests_per_poll=2,
        allow_high_tier_rate_limits=True,
    )
    state = _state({
        CHANNEL: {"initialized": True, "oldest": "1.0"},
        other: {"initialized": True, "oldest": "1.0"},
    })
    responses = [
        {"ok": False, "error": "channel_not_found"},
        {"ok": True, "messages": [_message("2.0", f"hi <@{USER}>")]},
    ]

    with patch.object(scanner, "_api", side_effect=responses):
        pollen, watermark = scanner.poll(config, state)

    updated = json.loads(watermark)
    assert [item["metadata"]["channel_id"] for item in pollen] == [other]
    assert updated["channels"][CHANNEL]["oldest"] == "1.0"
    assert updated["channels"][other]["oldest"] == "2.0"


def test_persistent_channel_error_does_not_starve_next_channel(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    other = "C002"
    config = _config(
        scanner, watch_channels=[CHANNEL, other], watch_dms=False,
    )
    called = []

    def fake(method, token, params=None):
        called.append(params["channel"])
        return {"ok": False, "error": "channel_not_found"}

    with patch.object(scanner, "_api", side_effect=fake):
        _, watermark = scanner.poll(config, _state())
        scanner.poll(config, watermark)

    assert called == [CHANNEL, other]


def test_invalid_current_state_and_config_fail_before_network(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    invalid_states = [
        json.dumps({
            "version": 2,
            "channels": {CHANNEL: {"initialized": "true", "oldest": "1.0"}},
            "next_index": 0,
            "dm_channels": [],
            "dm_refreshed_at": "",
        }),
        json.dumps({
            "version": 2,
            "channels": {CHANNEL: {"initialized": True, "oldest": "1e3"}},
            "next_index": 0,
            "dm_channels": [],
            "dm_refreshed_at": "",
        }),
        _state()[:-1] + ',"unexpected":true}',
    ]
    with patch.object(scanner, "_api") as api:
        for state in invalid_states:
            assert scanner.poll(_config(scanner, watch_dms=False), state) == ([], state)
    api.assert_not_called()

    invalid_configs = [
        _config(scanner, watch_channels=CHANNEL, watch_dms=False),
        _config(scanner, watch_channels=[CHANNEL, CHANNEL], watch_dms=False),
        _config(scanner, user_id=[USER], watch_dms=False),
        _config(scanner, token_env=123, watch_dms=False),
        _config(scanner, allow_high_tier_rate_limits=1, watch_dms=False),
    ]
    with patch.object(scanner, "_api") as api:
        for config in invalid_configs:
            assert scanner.poll(config, "") == ([], "")
    api.assert_not_called()


def test_malformed_late_message_and_wrong_order_do_not_commit(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    config = _config(scanner, watch_channels=[CHANNEL], watch_dms=False)
    state = _state({CHANNEL: {"initialized": True, "oldest": "1.0"}})
    malformed = _message("2.0")
    malformed["user_profile"]["real_name"] = {"bad": "shape"}
    batches = [
        [_message("3.0", f"hi <@{USER}>"), malformed],
        [_message("2.0"), _message("3.0")],
        [_message("2.0"), _message("2.0")],
    ]
    for messages in batches:
        with patch.object(
            scanner,
            "_api",
            return_value={"ok": True, "messages": messages},
        ):
            assert scanner.poll(config, state) == ([], state)


def test_contradictory_or_repeated_history_cursor_is_not_committed(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    config = _config(scanner, watch_channels=[CHANNEL], watch_dms=False)
    cursor_state = _state({
        CHANNEL: {
            "initialized": True,
            "oldest": "1.0",
            "cursor": "same-cursor",
            "pending_highest": "3.0",
        }
    })
    responses = [
        {
            "ok": True,
            "messages": [],
            "has_more": True,
            "response_metadata": {"next_cursor": "same-cursor"},
        },
        {
            "ok": True,
            "messages": [],
            "has_more": False,
            "response_metadata": {"next_cursor": "unexpected"},
        },
    ]
    for response in responses:
        with patch.object(scanner, "_api", return_value=response):
            assert scanner.poll(config, cursor_state) == ([], cursor_state)


def test_legacy_bootstrap_never_moves_boundary_backwards(monkeypatch):
    scanner = SlackScanner()
    monkeypatch.setenv("SLACK_TOKEN", "token")
    config = _config(scanner, watch_channels=[CHANNEL], watch_dms=False)
    legacy = "2026-01-01T00:00:00Z"
    with patch.object(
        scanner,
        "_api",
        return_value={"ok": True, "messages": [_message("1.0")]},
    ):
        pollen, watermark = scanner.poll(config, legacy)
    assert pollen == []
    assert Decimal(json.loads(watermark)["channels"][CHANNEL]["oldest"]) > 1


def test_strict_json_rejects_duplicates_and_nonfinite_numbers():
    with pytest.raises(ValueError):
        _mod._strict_json('{"ok":true,"ok":false}')
    with pytest.raises(ValueError):
        _mod._strict_json('{"ok":true,"value":NaN}')
