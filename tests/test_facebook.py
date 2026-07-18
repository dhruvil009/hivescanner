"""Tests for the deliberately Page-Messenger-only Facebook adapter."""

import importlib.util
import json
import os
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "fb_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "facebook", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
FacebookScanner = _mod.FacebookScanner

PAGE = "123456789"
CONVERSATION = "t_100"


def _message(msg_id, *, sender="777", name="Alice", text="Hello"):
    return {
        "id": msg_id,
        "message": text,
        "from": {"id": sender, "name": name},
        "created_time": "2026-07-15T10:00:00+0000",
    }


def _state(*, seen=("old",), page=PAGE, conversation=CONVERSATION):
    return json.dumps({
        "version": 3,
        "initialized": True,
        "pages": {
            page: {
                "initialized": True,
                "conversations": {
                    conversation: {
                        "initialized": True,
                        "seen_messages": list(seen),
                    }
                },
            }
        },
    })


def _config(scanner, **changes):
    return {**scanner.configure(), "watch_pages": [PAGE], **changes}


def _graph(messages, *, conversations=None):
    conversations = conversations or [{"id": CONVERSATION}]

    def fake(endpoint, token, api_version, params=None):
        if endpoint == f"/{PAGE}/conversations":
            return {"data": conversations}
        if endpoint == f"/{CONVERSATION}/messages":
            return {"data": messages}
        return None

    return fake


def test_defaults_are_page_scoped_and_versioned():
    config = FacebookScanner().configure()
    assert config["token_env"] == "FACEBOOK_PAGE_TOKEN"
    assert config["api_version"] == "v25.0"
    assert config["watch_pages"] == []
    assert config["max_items"] == 100
    assert config["max_pages"] == 3
    assert config["pages_per_poll"] == 2
    assert config["conversations_per_page"] == 4


def test_missing_token_and_missing_page_scope_are_quiet(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.delenv("FACEBOOK_PAGE_TOKEN", raising=False)
    assert scanner.poll(_config(scanner), "safe") == ([], "safe")
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    assert scanner.poll(scanner.configure(), "safe") == ([], "safe")


def test_page_conversation_emits_only_new_inbound_message(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    messages = [_message("new"), _message("old")]
    with patch.object(scanner, "_graph", side_effect=_graph(messages)) as graph:
        pollen, watermark = scanner.poll(_config(scanner), _state())

    assert [item["id"] for item in pollen] == ["facebook-msg-new"]
    item = pollen[0]
    assert item["type"] == "facebook_message"
    assert item["author_name"] == "Alice"
    assert item["metadata"]["page_id"] == PAGE
    assert item["metadata"]["conversation_id"] == CONVERSATION
    assert json.loads(watermark)["pages"][PAGE]["conversations"][CONVERSATION][
        "seen_messages"
    ][0] == "new"
    assert all("notifications" not in call.args[0] for call in graph.call_args_list)


def test_outbound_page_messages_are_suppressed_but_checkpointed(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    messages = [_message("outbound", sender=PAGE), _message("old")]
    with patch.object(scanner, "_graph", side_effect=_graph(messages)):
        pollen, watermark = scanner.poll(_config(scanner), _state())
    assert pollen == []
    retained = json.loads(watermark)["pages"][PAGE]["conversations"][CONVERSATION][
        "seen_messages"
    ]
    assert retained[0] == "outbound"


def test_first_install_is_quiet_and_snapshots_only_active_messages(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    with patch.object(scanner, "_graph", side_effect=_graph([_message("historical")])):
        pollen, watermark = scanner.poll(_config(scanner), "")
    assert pollen == []
    state = json.loads(watermark)
    assert state["pages"][PAGE]["initialized"] is True
    assert state["pages"][PAGE]["conversations"][CONVERSATION]["seen_messages"] == [
        "historical"
    ]


def test_new_conversation_after_bootstrap_can_surface_current_inbound_message(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    state = json.dumps({
        "version": 3,
        "initialized": True,
        "pages": {PAGE: {"initialized": True, "conversations": {}}},
    })
    with patch.object(scanner, "_graph", side_effect=_graph([_message("first")])):
        pollen, _ = scanner.poll(_config(scanner), state)
    assert [item["id"] for item in pollen] == ["facebook-msg-first"]


def test_cursor_pagination_is_followed_for_conversations(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    calls = []

    def fake(endpoint, token, api_version, params=None):
        calls.append((endpoint, dict(params or {})))
        if endpoint == f"/{PAGE}/conversations":
            if params.get("after") == "next":
                return {"data": []}
            return {
                "data": [{"id": CONVERSATION}],
                "paging": {"next": "url", "cursors": {"after": "next"}},
            }
        return {"data": [_message("old")]}

    with patch.object(scanner, "_graph", side_effect=fake):
        scanner.poll(_config(scanner), _state())
    conversation_calls = [params for endpoint, params in calls if endpoint.endswith("/conversations")]
    assert conversation_calls == [
        {"fields": "id,updated_time,participants", "limit": 100, "platform": "messenger"},
        {"fields": "id,updated_time,participants", "limit": 100, "platform": "messenger", "after": "next"},
    ]


def test_errors_and_invalid_page_or_version_preserve_watermark(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    assert scanner.poll(_config(scanner, api_version="latest"), "safe") == ([], "safe")
    assert scanner.poll(_config(scanner, watch_pages=["../../me"]), "safe") == ([], "safe")
    with patch.object(scanner, "_graph", return_value=None):
        assert scanner.poll(_config(scanner), "safe") == ([], "safe")


def test_active_conversation_limit_fails_closed(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    conversations = [{"id": f"c{i}"} for i in range(101)]
    with patch.object(scanner, "_graph", side_effect=_graph([], conversations=conversations)):
        assert scanner.poll(_config(scanner), "safe") == ([], "safe")


def test_conversation_budget_rotates_without_starvation(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    conversation_ids = [f"c{index}" for index in range(5)]
    state = json.dumps({
        "version": 4,
        "initialized": True,
        "next_page_index": 0,
        "pages": {
            PAGE: {
                "initialized": True,
                "next_conversation_index": 0,
                "conversations": {
                    value: {"initialized": True, "seen_messages": ["old"]}
                    for value in conversation_ids
                },
            }
        },
    })
    called_messages = []

    def fake(endpoint, token, api_version, params=None):
        if endpoint.endswith("/conversations"):
            return {"data": [{"id": value} for value in conversation_ids]}
        called_messages.append(endpoint)
        return {"data": [_message("old")]}

    config = _config(scanner, conversations_per_page=2)
    with patch.object(scanner, "_graph", side_effect=fake):
        _, state = scanner.poll(config, state)
        scanner.poll(config, state)

    assert called_messages == ["/c0/messages", "/c1/messages", "/c2/messages", "/c3/messages"]


def test_one_conversation_failure_does_not_rollback_another(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    conversations = ["c0", "c1"]
    state = json.dumps({
        "version": 4,
        "initialized": True,
        "next_page_index": 0,
        "pages": {
            PAGE: {
                "initialized": True,
                "next_conversation_index": 0,
                "conversations": {
                    value: {"initialized": True, "seen_messages": ["old"]}
                    for value in conversations
                },
            }
        },
    })

    def fake(endpoint, token, api_version, params=None):
        if endpoint.endswith("/conversations"):
            return {"data": [{"id": value} for value in conversations]}
        if endpoint == "/c0/messages":
            return None
        return {"data": [_message("new"), _message("old")]}

    with patch.object(scanner, "_graph", side_effect=fake):
        pollen, watermark = scanner.poll(
            _config(scanner, conversations_per_page=2), state
        )

    assert [item["id"] for item in pollen] == ["facebook-msg-new"]
    updated = json.loads(watermark)["pages"][PAGE]["conversations"]
    assert updated["c0"]["seen_messages"] == ["old"]
    assert updated["c1"]["seen_messages"][0] == "new"


def test_unselected_bootstrap_conversations_remain_quiet_when_first_visited(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    conversations = [{"id": "c0"}, {"id": "c1"}]
    visited = []

    def fake(endpoint, token, api_version, params=None):
        if endpoint.endswith("/conversations"):
            return {"data": conversations}
        visited.append(endpoint)
        return {"data": [_message(f"history-{endpoint[1:3]}")]}

    config = _config(scanner, conversations_per_page=1)
    with patch.object(scanner, "_graph", side_effect=fake):
        first, watermark = scanner.poll(config, "")
        second, _ = scanner.poll(config, watermark)

    assert first == [] and second == []
    assert visited == ["/c0/messages", "/c1/messages"]


def test_new_conversation_drains_bounded_backlog_instead_of_dropping_it(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    state = json.dumps({
        "version": 4,
        "initialized": True,
        "next_page_index": 0,
        "pages": {
            PAGE: {
                "initialized": True,
                "next_conversation_index": 0,
                "conversations": {},
            }
        },
    })
    message_calls = []

    def fake(endpoint, token, api_version, params=None):
        if endpoint.endswith("/conversations"):
            return {"data": [{"id": CONVERSATION}]}
        message_calls.append(dict(params or {}))
        if params.get("after") == "second":
            return {"data": [_message("m1")]}
        return {
            "data": [_message("m3"), _message("m2")],
            "paging": {"next": "url", "cursors": {"after": "second"}},
        }

    with patch.object(scanner, "_graph", side_effect=fake):
        pollen, _ = scanner.poll(_config(scanner, max_items=2), state)
    assert [item["id"] for item in pollen] == [
        "facebook-msg-m3",
        "facebook-msg-m2",
        "facebook-msg-m1",
    ]
    assert message_calls[1]["after"] == "second"


def test_malformed_message_and_current_state_fail_closed(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    state_value = json.loads(_state())
    state_value["version"] = 4
    state_value["next_page_index"] = 0
    state_value["pages"][PAGE]["next_conversation_index"] = 0
    state = json.dumps(state_value, sort_keys=True, separators=(",", ":"))
    malformed = _message("new")
    malformed["from"] = {"id": {"bad": "shape"}, "name": "Alice"}
    with patch.object(scanner, "_graph", side_effect=_graph([malformed])):
        assert scanner.poll(_config(scanner), state) == ([], state)

    corrupt = json.loads(state)
    corrupt["pages"][PAGE]["conversations"][CONVERSATION]["initialized"] = "true"
    corrupt_state = json.dumps(corrupt)
    with patch.object(scanner, "_graph") as graph:
        assert scanner.poll(_config(scanner), corrupt_state) == ([], corrupt_state)
    graph.assert_not_called()


def test_config_collections_are_not_silently_coerced(monkeypatch):
    scanner = FacebookScanner()
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "token")
    for config in (
        _config(scanner, watch_pages=PAGE),
        _config(scanner, watch_pages=[123456789]),
        _config(scanner, token_env=123),
        _config(scanner, api_version={"version": "v25.0"}),
    ):
        with patch.object(scanner, "_graph") as graph:
            assert scanner.poll(config, "safe") == ([], "safe")
        graph.assert_not_called()
