"""Contract and state-machine tests for the Twitter/X scanner."""

import importlib.util
import json
import os
import sys
from unittest.mock import patch

import pytest


_TWITTER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "community", "twitter", "adapter.py"
)
_spec = importlib.util.spec_from_file_location("twitter_adapter", _TWITTER_PATH)
_twitter_mod = importlib.util.module_from_spec(_spec)
sys.modules["twitter_adapter"] = _twitter_mod
_spec.loader.exec_module(_twitter_mod)
TwitterScanner = _twitter_mod.TwitterScanner

USER_ID = "2244994945"
AUTHOR_ID = "1234567890"
SENDER_ID = "9876543210"


def _state(**overrides):
    value = {
        "version": 4,
        "mentions_initialized": True,
        "mention_user_id": USER_ID,
        "mention_since_id": "100",
        "mention_cursor": "",
        "mention_pending_highest": "",
        "dms_initialized": True,
        "dm_user_id": USER_ID,
        "seen_dms": ["100"],
        "dm_cursor": "",
        "dm_pending_ids": [],
    }
    value.update(overrides)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _config(**overrides):
    value = TwitterScanner().configure()
    value.update(overrides)
    return value


def _mention(tweet_id="200", author_id=AUTHOR_ID, text="Hey @hive"):
    return {
        "id": tweet_id,
        "author_id": author_id,
        "text": text,
        "created_at": "2026-07-15T10:00:00.000Z",
    }


def _mention_response(tweet_id="200", *, next_token="", errors=None):
    result = {
        "data": [_mention(tweet_id)],
        "includes": {
            "users": [
                {"id": AUTHOR_ID, "username": "alice", "name": "Alice Smith"}
            ]
        },
        "meta": {"result_count": 1},
    }
    if next_token:
        result["meta"]["next_token"] = next_token
    if errors is not None:
        result["errors"] = errors
    return result


def _dm(event_id="200", sender_id=SENDER_ID, text="Can we chat?"):
    return {
        "event_type": "MessageCreate",
        "id": event_id,
        "sender_id": sender_id,
        "text": text,
        "created_at": "2026-07-15T11:00:00.000Z",
        "dm_conversation_id": f"{USER_ID}-{SENDER_ID}",
    }


@pytest.fixture
def scanner():
    return TwitterScanner()


def test_configure_matches_paid_dm_opt_in_defaults(scanner):
    config = scanner.configure()
    assert config == {
        "enabled": False,
        "token_env": "TWITTER_BEARER_TOKEN",
        "dm_token_env": "TWITTER_USER_TOKEN",
        "username": "",
        "user_id": "",
        "watch_mentions": True,
        "watch_dms": False,
        "max_items": 100,
        "max_pages": 10,
    }


@pytest.mark.parametrize("user_id", ["", "not-numeric", "1" * 20])
def test_invalid_user_id_fails_closed_without_api_call(scanner, user_id):
    with patch.object(scanner, "_api") as api:
        pollen, watermark = scanner.poll(_config(user_id=user_id), "opaque")
    assert pollen == []
    assert watermark == "opaque"
    api.assert_not_called()


@pytest.mark.parametrize("field", ["token_env", "dm_token_env"])
def test_invalid_environment_variable_name_fails_closed(scanner, field):
    with patch.object(scanner, "_api") as api:
        pollen, watermark = scanner.poll(
            _config(user_id=USER_ID, **{field: "../../SECRET"}), _state()
        )
    assert pollen == []
    assert watermark == _state()
    api.assert_not_called()


def test_missing_mention_token_does_not_advance_initialized_state(scanner):
    with patch.dict(os.environ, {}, clear=True):
        pollen, watermark = scanner.poll(_config(user_id=USER_ID), _state())
    assert pollen == []
    assert watermark == _state()


def test_first_enable_snapshots_mentions_and_dms_without_alerting(scanner):
    responses = [
        _mention_response("300"),
        {"data": [_dm("400")], "meta": {"result_count": 1}},
    ]
    with patch.dict(
        os.environ,
        {"TWITTER_BEARER_TOKEN": "public", "TWITTER_USER_TOKEN": "user"},
        clear=True,
    ), patch.object(scanner, "_api", side_effect=responses):
        pollen, watermark = scanner.poll(
            _config(user_id=USER_ID, watch_dms=True), ""
        )
    state = json.loads(watermark)
    assert pollen == []
    assert state["mention_since_id"] == "300"
    assert state["seen_dms"] == ["400"]
    assert state["mentions_initialized"] is True
    assert state["dms_initialized"] is True


def test_initialized_mention_uses_real_x_shape_and_emits(scanner):
    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(scanner, "_api", return_value=_mention_response("200")) as api:
        pollen, watermark = scanner.poll(_config(user_id=USER_ID), _state())

    assert len(pollen) == 1
    item = pollen[0]
    assert item["id"] == "twitter-mention-200"
    assert item["type"] == "twitter_mention"
    assert item["author"] == "alice"
    assert item["author_name"] == "Alice Smith"
    assert item["url"] == "https://x.com/alice/status/200"
    assert item["metadata"]["author_id"] == AUTHOR_ID
    path, params, token = api.call_args.args
    assert path == f"users/{USER_ID}/mentions"
    assert params["since_id"] == "100"
    assert token == "public"
    assert json.loads(watermark)["mention_since_id"] == "200"


def test_self_authored_mention_is_suppressed_but_advances(scanner):
    response = _mention_response("200")
    response["data"][0]["author_id"] = USER_ID
    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(scanner, "_api", return_value=response):
        pollen, watermark = scanner.poll(_config(user_id=USER_ID), _state())
    assert pollen == []
    assert json.loads(watermark)["mention_since_id"] == "200"


def test_mention_backlog_continues_without_advancing_since_id_early(scanner):
    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(
        scanner, "_api", return_value=_mention_response("300", next_token="page-two")
    ):
        first_pollen, first_watermark = scanner.poll(
            _config(user_id=USER_ID, max_pages=1), _state()
        )
    first_state = json.loads(first_watermark)
    assert [item["id"] for item in first_pollen] == ["twitter-mention-300"]
    assert first_state["mention_since_id"] == "100"
    assert first_state["mention_pending_highest"] == "300"
    assert first_state["mention_cursor"] == "page-two"

    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(scanner, "_api", return_value=_mention_response("200")) as api:
        second_pollen, second_watermark = scanner.poll(
            _config(user_id=USER_ID, max_pages=1), first_watermark
        )
    assert [item["id"] for item in second_pollen] == ["twitter-mention-200"]
    assert api.call_args.args[1]["pagination_token"] == "page-two"
    second_state = json.loads(second_watermark)
    assert second_state["mention_since_id"] == "300"
    assert second_state["mention_cursor"] == ""


def test_mention_failure_mid_window_commits_completed_page_cursor(scanner):
    original = _state()
    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(
        scanner,
        "_api",
        side_effect=[_mention_response("300", next_token="next"), None],
    ):
        pollen, watermark = scanner.poll(
            _config(user_id=USER_ID, max_pages=2), original
        )
    assert [item["id"] for item in pollen] == ["twitter-mention-300"]
    assert json.loads(watermark)["mention_since_id"] == "100"
    assert json.loads(watermark)["mention_cursor"] == "next"
    assert json.loads(watermark)["mention_pending_highest"] == "300"

    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(scanner, "_api", return_value=_mention_response("200")) as api:
        resumed, final_watermark = scanner.poll(
            _config(user_id=USER_ID, max_pages=1), watermark
        )
    assert [item["id"] for item in resumed] == ["twitter-mention-200"]
    assert api.call_args.args[1]["pagination_token"] == "next"
    assert json.loads(final_watermark)["mention_since_id"] == "300"


def test_partial_x_response_with_data_is_processed(scanner):
    response = _mention_response(
        "200", errors=[{"title": "Partial Error", "detail": "one expansion missing"}]
    )
    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(scanner, "_api", return_value=response):
        pollen, watermark = scanner.poll(_config(user_id=USER_ID), _state())
    assert [item["id"] for item in pollen] == ["twitter-mention-200"]
    assert json.loads(watermark)["mention_since_id"] == "200"


def test_dm_requires_separate_user_context_token(scanner):
    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(scanner, "_api", return_value={"meta": {}}) as api:
        pollen, watermark = scanner.poll(
            _config(user_id=USER_ID, watch_mentions=False, watch_dms=True), _state()
        )
    assert pollen == []
    assert json.loads(watermark)["seen_dms"] == ["100"]
    api.assert_not_called()


def test_initialized_dm_uses_user_token_and_suppresses_self(scanner):
    response = {
        "data": [_dm("300"), _dm("200", sender_id=USER_ID)],
        "meta": {"result_count": 2},
    }
    with patch.dict(
        os.environ, {"TWITTER_USER_TOKEN": "user"}, clear=True
    ), patch.object(scanner, "_api", return_value=response) as api:
        pollen, watermark = scanner.poll(
            _config(user_id=USER_ID, watch_mentions=False, watch_dms=True), _state()
        )
    assert [item["id"] for item in pollen] == ["twitter-dm-300"]
    assert pollen[0]["metadata"]["conversation_id"] == f"{USER_ID}-{SENDER_ID}"
    path, params, token = api.call_args.args
    assert path == "dm_events"
    assert params["event_types"] == "MessageCreate"
    assert token == "user"
    assert json.loads(watermark)["seen_dms"] == ["100", "200", "300"]


def test_non_message_or_malformed_dm_page_fails_closed(scanner):
    joined = _dm("300")
    joined["event_type"] = "ParticipantsJoin"
    response = {
        "data": [joined, _dm("not-an-id"), _dm("200")],
        "meta": {},
    }
    with patch.dict(
        os.environ, {"TWITTER_USER_TOKEN": "user"}, clear=True
    ), patch.object(scanner, "_api", return_value=response):
        pollen, watermark = scanner.poll(
            _config(user_id=USER_ID, watch_mentions=False, watch_dms=True), _state()
        )
    assert pollen == []
    assert watermark == _state()


def test_dm_backlog_carries_pending_ids_until_known_boundary(scanner):
    with patch.dict(
        os.environ, {"TWITTER_USER_TOKEN": "user"}, clear=True
    ), patch.object(
        scanner,
        "_api",
        return_value={"data": [_dm("400")], "meta": {"next_token": "older"}},
    ):
        first_pollen, first_watermark = scanner.poll(
            _config(
                user_id=USER_ID,
                watch_mentions=False,
                watch_dms=True,
                max_pages=1,
            ),
            _state(),
        )
    first_state = json.loads(first_watermark)
    assert [item["id"] for item in first_pollen] == ["twitter-dm-400"]
    assert first_state["seen_dms"] == ["100"]
    assert first_state["dm_pending_ids"] == ["400"]
    assert first_state["dm_cursor"] == "older"

    response = {"data": [_dm("300"), _dm("100")], "meta": {}}
    with patch.dict(
        os.environ, {"TWITTER_USER_TOKEN": "user"}, clear=True
    ), patch.object(scanner, "_api", return_value=response) as api:
        second_pollen, second_watermark = scanner.poll(
            _config(
                user_id=USER_ID,
                watch_mentions=False,
                watch_dms=True,
                max_pages=1,
            ),
            first_watermark,
        )
    assert [item["id"] for item in second_pollen] == ["twitter-dm-300"]
    assert api.call_args.args[1]["pagination_token"] == "older"
    second_state = json.loads(second_watermark)
    assert second_state["seen_dms"] == ["100", "300", "400"]
    assert second_state["dm_cursor"] == ""
    assert second_state["dm_pending_ids"] == []


def test_component_failure_does_not_block_other_component_progress(scanner):
    response = {"data": [_dm("200")], "meta": {}}
    with patch.dict(
        os.environ,
        {"TWITTER_BEARER_TOKEN": "public", "TWITTER_USER_TOKEN": "user"},
        clear=True,
    ), patch.object(scanner, "_api", side_effect=[None, response]):
        pollen, watermark = scanner.poll(
            _config(user_id=USER_ID, watch_dms=True), _state()
        )
    state = json.loads(watermark)
    assert [item["id"] for item in pollen] == ["twitter-dm-200"]
    assert state["mention_since_id"] == "100"
    assert state["seen_dms"] == ["100", "200"]


def test_switching_user_rebootstraps_both_streams_quietly(scanner):
    new_user = "44196397"
    responses = [
        _mention_response("500"),
        {"data": [_dm("600")], "meta": {}},
    ]
    with patch.dict(
        os.environ,
        {"TWITTER_BEARER_TOKEN": "public", "TWITTER_USER_TOKEN": "user"},
        clear=True,
    ), patch.object(scanner, "_api", side_effect=responses):
        pollen, watermark = scanner.poll(
            _config(user_id=new_user, watch_dms=True), _state()
        )
    state = json.loads(watermark)
    assert pollen == []
    assert state["mention_user_id"] == new_user
    assert state["dm_user_id"] == new_user
    assert state["mention_since_id"] == "500"
    assert state["seen_dms"] == ["600"]


def test_state_loader_rejects_oversized_or_malformed_state(scanner):
    state = json.loads(_state())
    state["seen_dms"] = ["bad"] + [str(value) for value in range(1, 6002)]
    state["dm_pending_ids"] = ["bad", "200"]
    state["mention_since_id"] = "x"
    state["mention_cursor"] = "x\nheader"
    loaded = scanner._load_state(json.dumps(state))
    assert loaded is None


def test_malformed_api_collection_does_not_advance_component(scanner):
    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(scanner, "_api", return_value={"data": {"id": "200"}}):
        pollen, watermark = scanner.poll(_config(user_id=USER_ID), _state())
    assert pollen == []
    assert json.loads(watermark)["mention_since_id"] == "100"


def test_username_is_resolved_once_then_cached(scanner):
    config = _config(user_id="", username="Hive_User")
    responses = [
        {"data": {"id": USER_ID, "username": "hive_user"}},
        _mention_response("300"),
    ]
    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(scanner, "_api", side_effect=responses) as api:
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    assert api.call_args_list[0].args[0] == "users/by/username/Hive_User"
    first_state = json.loads(watermark)
    assert first_state["resolved_username"] == "Hive_User"
    assert first_state["resolved_user_id"] == USER_ID
    assert first_state["mention_since_id"] == "300"

    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(scanner, "_api", return_value=_mention_response("400")) as api:
        pollen, watermark = scanner.poll(config, watermark)
    assert [item["id"] for item in pollen] == ["twitter-mention-400"]
    assert api.call_count == 1
    assert api.call_args.args[0] == f"users/{USER_ID}/mentions"


def test_username_resolution_must_match_requested_username(scanner):
    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(
        scanner,
        "_api",
        return_value={"data": {"id": USER_ID, "username": "someone_else"}},
    ) as api:
        pollen, watermark = scanner.poll(
            _config(user_id="", username="Hive_User"), ""
        )
    assert pollen == []
    assert watermark == ""
    assert api.call_count == 1


@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
@pytest.mark.parametrize("field", ["watch_mentions", "watch_dms"])
def test_watch_flags_require_real_booleans(scanner, field, value):
    with patch.object(scanner, "_api") as api:
        pollen, watermark = scanner.poll(
            _config(user_id=USER_ID, **{field: value}), _state()
        )
    assert pollen == []
    assert watermark == _state()
    api.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_items", True),
        ("max_items", "100"),
        ("max_items", 4),
        ("max_items", 101),
        ("max_pages", False),
        ("max_pages", "2"),
        ("max_pages", 0),
        ("max_pages", 11),
    ],
)
def test_pagination_config_is_strict(scanner, field, value):
    with patch.object(scanner, "_api") as api:
        pollen, watermark = scanner.poll(
            _config(user_id=USER_ID, **{field: value}), _state()
        )
    assert pollen == []
    assert watermark == _state()
    api.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.pop("id"),
        lambda item: item.update(id="bad"),
        lambda item: item.pop("author_id"),
        lambda item: item.update(text=7),
        lambda item: item.update(created_at="yesterday"),
    ],
)
def test_malformed_mention_entry_fails_whole_page(scanner, mutation):
    response = _mention_response("200")
    mutation(response["data"][0])
    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(scanner, "_api", return_value=response):
        pollen, watermark = scanner.poll(_config(user_id=USER_ID), _state())
    assert pollen == []
    assert watermark == _state()


def test_malformed_next_token_fails_whole_page(scanner):
    response = _mention_response("200")
    response["meta"]["next_token"] = "bad\nheader"
    with patch.dict(
        os.environ, {"TWITTER_BEARER_TOKEN": "public"}, clear=True
    ), patch.object(scanner, "_api", return_value=response):
        pollen, watermark = scanner.poll(_config(user_id=USER_ID), _state())
    assert pollen == []
    assert watermark == _state()


def test_attachment_only_dm_has_nonempty_title(scanner):
    event = _dm("200")
    event.pop("text")
    with patch.dict(
        os.environ, {"TWITTER_USER_TOKEN": "user"}, clear=True
    ), patch.object(
        scanner, "_api", return_value={"data": [event], "meta": {"result_count": 1}}
    ):
        pollen, watermark = scanner.poll(
            _config(user_id=USER_ID, watch_mentions=False, watch_dms=True), _state()
        )
    assert pollen[0]["title"] == "Direct message with non-text content"
    assert json.loads(watermark)["seen_dms"] == ["100", "200"]


def test_reordered_dm_page_fails_closed(scanner):
    response = {"data": [_dm("200"), _dm("300")], "meta": {"result_count": 2}}
    with patch.dict(
        os.environ, {"TWITTER_USER_TOKEN": "user"}, clear=True
    ), patch.object(scanner, "_api", return_value=response):
        pollen, watermark = scanner.poll(
            _config(user_id=USER_ID, watch_mentions=False, watch_dms=True), _state()
        )
    assert pollen == []
    assert watermark == _state()


def test_dm_rate_limit_allows_only_one_page_per_poll(scanner):
    response = {
        "data": [_dm("200")],
        "meta": {"result_count": 1, "next_token": "a" * 16},
    }
    with patch.dict(
        os.environ, {"TWITTER_USER_TOKEN": "user"}, clear=True
    ), patch.object(scanner, "_api", return_value=response) as api:
        pollen, watermark = scanner.poll(
            _config(
                user_id=USER_ID,
                watch_mentions=False,
                watch_dms=True,
                max_pages=10,
            ),
            _state(),
        )
    assert [item["id"] for item in pollen] == ["twitter-dm-200"]
    assert api.call_count == 1
    assert json.loads(watermark)["dm_cursor"] == "a" * 16


def test_state_loader_rejects_duplicate_json_keys(scanner):
    assert scanner._load_state('{"version":4,"version":4}') is None
