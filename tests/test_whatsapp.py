"""Functional tests for whatsapp-cli's envelope and newest-first paging."""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
_PATH = os.path.join(os.path.dirname(__file__), "..", "workers", "sources", "whatsapp.py")
_spec = importlib.util.spec_from_file_location("whatsapp", _PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["whatsapp"] = _mod
_spec.loader.exec_module(_mod)
WhatsAppScanner = _mod.WhatsAppScanner

_NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _timestamp(minutes):
    return (_NOW + timedelta(minutes=minutes)).isoformat()


def _message(
    msg_id,
    minutes,
    *,
    chat="group-abc@g.us",
    sender="14155551234@s.whatsapp.net",
    content="Hello everyone!",
    from_me=False,
):
    return {
        "id": msg_id,
        "chat_jid": chat,
        "chat_name": "Dev Team",
        "sender": sender,
        "content": content,
        "timestamp": _timestamp(minutes),
        "media_type": "",
        "is_from_me": from_me,
    }


def _scanner(*, ready=False, boundary=None, boundary_ids=None):
    with (
        patch.object(_mod, "load_snapshot", return_value={}),
        patch.object(_mod, "snapshot_exists", return_value=ready),
    ):
        scanner = WhatsAppScanner()
    scanner._cli_available = True
    if ready:
        raw_boundary_ids = boundary_ids or ["old"]
        scanner._snapshot = {
            "schema_version": 4,
            "committed": {
                "initialized": True,
                "boundary_time": scanner._normalize_timestamp(boundary or _timestamp(-10)),
                "ids_at_boundary": [
                    scanner._message_key("group-abc@g.us", msg_id)
                    for msg_id in raw_boundary_ids
                ],
                "legacy_ids_at_boundary": [],
            },
            "candidate": {},
            "candidate_watermark": "",
            "bootstrap_pending": False,
        }
    return scanner


def _envelope(messages, *, success=True, error=None):
    return json.dumps({"success": success, "data": messages, "error": error})


def _poll(scanner, config, responses, watermark="2026-07-15T00:00:00Z"):
    iterator = iter(responses)
    with (
        patch.object(scanner, "_wa", side_effect=lambda *args, **kwargs: next(iterator)),
        patch.object(_mod, "save_snapshot"),
    ):
        return scanner.poll(config, watermark)


REQUIRED_KEYS = {
    "id", "source", "type", "title", "preview", "discovered_at",
    "author", "author_name", "group", "url", "metadata",
}


def test_configure_exposes_bounded_paging_and_store_path():
    config = _scanner().configure()
    assert config["enabled"] is False
    assert config["watch_chats"] == []
    assert config["max_messages"] == 20
    assert config["max_pages_per_poll"] == 100
    assert config["store_path"] == ""


def test_missing_cli_preserves_watermark():
    scanner = _scanner()
    scanner._cli_available = False
    watermark = "2026-07-15T00:00:00Z"
    assert scanner.poll(scanner.configure(), watermark) == ([], watermark)


def test_stable_cli_envelope_emits_new_incoming_message():
    scanner = _scanner(ready=True)
    pollen, _ = _poll(
        scanner,
        scanner.configure(),
        [_envelope([_message("msg001", -1)])],
    )
    assert len(pollen) == 1
    item = pollen[0]
    assert item["id"] == f"whatsapp-{scanner._message_key('group-abc@g.us', 'msg001')}"
    assert item["type"] == "whatsapp_message"
    assert item["author_name"] == ""
    assert item["group"] == "Dev Team"
    assert REQUIRED_KEYS <= item.keys()


def test_watch_filter_and_from_me_filter_are_applied():
    scanner = _scanner(ready=True)
    messages = [
        _message("wanted", -1, chat="group-abc@g.us"),
        _message("other", -1, chat="group-other@g.us"),
        _message("self", -1, chat="group-abc@g.us", from_me=True),
    ]
    config = {**scanner.configure(), "watch_chats": ["group-abc@g.us"]}
    pollen, _ = _poll(scanner, config, [_envelope(messages)])
    assert [item["metadata"]["msg_id"] for item in pollen] == ["wanted"]


def test_first_poll_is_quiet_and_stages_exact_boundary():
    scanner = _scanner()
    newest = _message("newest", -1)
    older = _message("older", -2)
    pollen, watermark = _poll(
        scanner,
        scanner.configure(),
        [_envelope([newest, older])],
        watermark="",
    )
    assert pollen == []
    assert scanner._snapshot["bootstrap_pending"] is True
    assert scanner._snapshot["candidate"]["ids_at_boundary"] == [
        scanner._message_key("group-abc@g.us", "newest")
    ]
    assert scanner._snapshot["candidate_watermark"] == watermark


def test_equal_timestamp_delayed_message_within_budget_is_not_skipped():
    shared = _timestamp(-5)
    scanner = _scanner(ready=True, boundary=shared, boundary_ids=["known"])
    newer = {**_message("newer", -1), "timestamp": _timestamp(-1)}
    known = {**_message("known", -5), "timestamp": shared}
    delayed = {**_message("delayed", -5), "timestamp": shared}
    older = {**_message("older", -6), "timestamp": _timestamp(-6)}
    config = {**scanner.configure(), "max_messages": 2, "max_pages_per_poll": 3}

    pollen, _ = _poll(
        scanner,
        config,
        [_envelope([newer, known, delayed, older])],
    )
    assert [item["metadata"]["msg_id"] for item in pollen] == [
        "newer", "delayed",
    ]


def test_boundary_ids_prevent_duplicates_during_overlap():
    boundary = _timestamp(-5)
    scanner = _scanner(ready=True, boundary=boundary, boundary_ids=["known"])
    known = {**_message("known", -5), "timestamp": boundary}
    pollen, _ = _poll(scanner, scanner.configure(), [_envelope([known])])
    assert pollen == []


def test_cli_error_and_invalid_timestamp_preserve_committed_watermark():
    scanner = _scanner(ready=True)
    watermark = "2026-07-15T00:00:00Z"
    assert _poll(
        scanner,
        scanner.configure(),
        [_envelope([], success=False, error="database locked")],
        watermark=watermark,
    ) == ([], watermark)

    future = _message("future", 60)
    pollen, _ = _poll(scanner, scanner.configure(), [_envelope([future])])
    assert pollen == []


def test_page_cap_preserves_watermark_instead_of_skipping_backlog():
    scanner = _scanner(ready=True)
    config = {**scanner.configure(), "max_messages": 1, "max_pages_per_poll": 1}
    old_watermark = "2026-07-15T00:00:00Z"
    pollen, watermark = _poll(
        scanner,
        config,
        [_envelope([_message("new", -1)])],
        watermark=old_watermark,
    )
    assert [item["metadata"]["msg_id"] for item in pollen] == ["new"]
    assert watermark == old_watermark


def test_store_path_is_passed_as_one_argument_not_shell_text():
    scanner = _scanner(ready=True)
    calls = []

    def fake(args, timeout=15):
        calls.append(args)
        return _envelope([])

    config = {**scanner.configure(), "store_path": "/tmp/store with spaces"}
    with patch.object(scanner, "_wa", side_effect=fake), patch.object(_mod, "save_snapshot"):
        scanner.poll(config, "2026-07-15T00:00:00Z")
    assert calls[0][:2] == ["--store", "/tmp/store with spaces"]


def test_poll_uses_one_bounded_query_instead_of_unstable_offset_pages():
    scanner = _scanner(ready=True)
    calls = []

    def fake(args, timeout=15):
        calls.append(args)
        return _envelope([])

    config = {**scanner.configure(), "max_messages": 25, "max_pages_per_poll": 4}
    with patch.object(scanner, "_wa", side_effect=fake), patch.object(_mod, "save_snapshot"):
        scanner.poll(config, "2026-07-15T00:00:00Z")
    assert len(calls) == 1
    assert calls[0][-4:] == ["--limit", "100", "--page", "0"]


def test_same_provider_id_in_two_chats_has_distinct_pollen_ids():
    scanner = _scanner(ready=True)
    messages = [
        _message("same", -1, chat="one@g.us"),
        _message("same", -1, chat="two@g.us"),
    ]
    pollen, _ = _poll(scanner, scanner.configure(), [_envelope(messages)])
    assert len(pollen) == 2
    assert len({item["id"] for item in pollen}) == 2


def test_malformed_or_reordered_cli_data_fails_closed():
    scanner = _scanner(ready=True)
    watermark = "2026-07-15T00:00:00Z"
    malformed = {**_message("bad", -1), "is_from_me": "false"}
    assert _poll(scanner, scanner.configure(), [_envelope([malformed])], watermark) == (
        [], watermark,
    )

    older = _message("older", -2)
    newer = _message("newer", -1)
    assert _poll(scanner, scanner.configure(), [_envelope([older, newer])], watermark) == (
        [], watermark,
    )


def test_current_cli_envelope_is_required_and_duplicate_json_keys_are_rejected():
    scanner = _scanner(ready=True)
    watermark = "2026-07-15T00:00:00Z"
    raw_array = json.dumps([_message("legacy", -1)])
    assert _poll(scanner, scanner.configure(), [raw_array], watermark) == ([], watermark)
    duplicate = '{"success":true,"success":true,"data":[],"error":null}'
    assert _poll(scanner, scanner.configure(), [duplicate], watermark) == ([], watermark)
    nonfinite = '{"success":true,"data":[],"error":NaN}'
    assert _poll(scanner, scanner.configure(), [nonfinite], watermark) == ([], watermark)


def test_schema_three_boundary_ids_migrate_without_duplicate_pollen():
    scanner = _scanner(ready=True)
    boundary = _timestamp(-5)
    scanner._snapshot = {
        "schema_version": 3,
        "committed": {
            "initialized": True,
            "boundary_time": scanner._normalize_timestamp(boundary),
            "ids_at_boundary": ["known"],
        },
        "candidate": {},
        "candidate_watermark": "",
        "bootstrap_pending": False,
    }
    known = {**_message("known", -5), "timestamp": boundary}
    pollen, _ = _poll(scanner, scanner.configure(), [_envelope([known])])
    assert pollen == []
    assert scanner._snapshot["schema_version"] == 4


def test_invalid_config_and_watermark_are_rejected_before_tool_or_query():
    scanner = _scanner(ready=True)
    scanner._cli_available = None
    watermark = "2026-07-15T00:00:00Z"
    invalid_configs = [
        {**scanner.configure(), "watch_chats": "group-abc@g.us"},
        {**scanner.configure(), "watch_chats": ["group-abc@g.us", "group-abc@g.us"]},
        {**scanner.configure(), "watch_chats": [""]},
        {**scanner.configure(), "max_messages": True},
        {**scanner.configure(), "store_path": " ./store"},
    ]
    with patch.object(_mod, "ensure_tool") as ensure:
        for config in invalid_configs:
            assert scanner.poll(config, watermark) == ([], watermark)
        assert scanner.poll(scanner.configure(), "not-a-time") == ([], "not-a-time")
    ensure.assert_not_called()


def test_unknown_or_corrupt_transaction_snapshot_fails_before_query():
    watermark = "2026-07-15T00:00:00Z"
    scanner = _scanner(ready=True)
    scanner._snapshot = {"schema_version": []}
    with patch.object(scanner, "_wa") as query:
        assert scanner.poll(scanner.configure(), watermark) == ([], watermark)
    query.assert_not_called()

    scanner = _scanner(ready=True)
    scanner._snapshot["candidate"] = {
        "initialized": True,
        "boundary_time": scanner._normalize_timestamp(_timestamp(-1)),
        "ids_at_boundary": [{}],
        "legacy_ids_at_boundary": [],
    }
    scanner._snapshot["candidate_watermark"] = "2026-07-15T00:00:01Z"
    with patch.object(scanner, "_wa") as query:
        assert scanner.poll(scanner.configure(), watermark) == ([], watermark)
    query.assert_not_called()


def test_staged_boundary_uses_actual_timestamp_comparison():
    scanner = _scanner(ready=True)
    staged_message = _message("staged", -1)
    staged_time = scanner._normalize_timestamp(staged_message["timestamp"])
    scanner._snapshot["candidate"] = {
        "initialized": True,
        "boundary_time": staged_time,
        "ids_at_boundary": [
            scanner._message_key(staged_message["chat_jid"], staged_message["id"])
        ],
        "legacy_ids_at_boundary": [],
    }
    scanner._snapshot["candidate_watermark"] = "2026-07-15T01:00:00+01:00"
    pollen, _ = _poll(
        scanner,
        scanner.configure(),
        [_envelope([staged_message])],
        watermark="2026-07-15T00:00:00Z",
    )
    assert pollen == []


def test_raw_legacy_message_snapshot_migrates_without_replay():
    scanner = _scanner(ready=True)
    boundary = scanner._normalize_timestamp(_timestamp(-5))
    scanner._snapshot = {"known": boundary}
    known = {**_message("known", -5), "timestamp": boundary}
    pollen, _ = _poll(
        scanner,
        scanner.configure(),
        [_envelope([known])],
        watermark=boundary,
    )
    assert pollen == []
    assert scanner._snapshot["schema_version"] == 4


def test_provider_cannot_exceed_limit_or_commit_after_late_malformed_message():
    scanner = _scanner(ready=True)
    watermark = "2026-07-15T00:00:00Z"
    config = {**scanner.configure(), "max_messages": 1, "max_pages_per_poll": 1}
    over_limit = [_message("one", -1), _message("two", -2)]
    assert _poll(scanner, config, [_envelope(over_limit)], watermark) == ([], watermark)

    malformed = {**_message("bad", -2), "content": "x" * 100_001}
    assert _poll(
        scanner,
        scanner.configure(),
        [_envelope([_message("valid", -1), malformed])],
        watermark,
    ) == ([], watermark)


def test_cli_output_must_be_utf8():
    scanner = _scanner()

    def invalid_utf8(*args, **kwargs):
        kwargs["stdout"].write(b"\xff")
        return SimpleNamespace(returncode=0)

    with patch.object(_mod.subprocess, "run", side_effect=invalid_utf8):
        assert scanner._wa(["messages", "list"]) is None
