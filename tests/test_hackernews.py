"""Functional tests for Hacker News Algolia searches and continuation state."""

import importlib.util
import json
import os
from datetime import datetime, timezone
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "hackernews_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "hackernews", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
HackerNewsScanner = _mod.HackerNewsScanner

STORY = {
    "objectID": "40001",
    "title": "Show HN: My New Tool",
    "author": "pg",
    "points": 250,
    "num_comments": 100,
}
MENTION = {
    "objectID": "40002",
    "title": "",
    "comment_text": "I agree with @dhruvil on this one",
    "author": "someone",
    "points": 5,
    "num_comments": 0,
}
KEY_SHOW = "show hn" + chr(0) + "100"
KEY_AI = "ai" + chr(0) + "100"
KEY_ML = "machine learning" + chr(0) + "100"


def _state(*, keyword_seen=None, username="", mention_seen=None, created=None):
    if created is None:
        created = int(datetime.now(timezone.utc).timestamp()) - 60
    return json.dumps({
        "version": 4,
        "keyword_seen": keyword_seen or {},
        "created_epoch": created if username else 0,
        "mention_username": username,
        "mention_initialized": bool(username),
        "mention_seen": mention_seen or [],
        "mention_page": 0,
        "mention_since_epoch": 0,
        "mention_until_epoch": 0,
        "mention_pending": [],
    })


def _result(hits, pages=1):
    return {"hits": hits, "nbPages": pages}


def test_defaults_bound_public_api_load():
    config = HackerNewsScanner().configure()
    assert config["watch_keywords"] == []
    assert config["username"] == ""
    assert config["min_points"] == 100
    assert config["max_items"] == 100
    assert config["max_pages"] == 3
    assert config["keywords_per_poll"] == 2


def test_keyword_first_poll_is_quiet_then_new_story_emits():
    scanner = HackerNewsScanner()
    config = {**scanner.configure(), "watch_keywords": ["Show HN"]}
    with patch.object(scanner, "_api_get", return_value=_result([STORY])):
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    assert json.loads(watermark)["keyword_seen"][KEY_SHOW] == ["40001"]

    newer = {**STORY, "objectID": "40003", "title": "Show HN: Another tool"}
    with patch.object(scanner, "_api_get", return_value=_result([newer, STORY])):
        pollen, _ = scanner.poll(config, watermark)
    assert [item["id"] for item in pollen] == ["hn-story-40003"]
    assert pollen[0]["metadata"]["keyword"] == "Show HN"


def test_duplicate_keyword_variants_and_overlapping_queries_emit_story_once():
    scanner = HackerNewsScanner()
    config = {
        **scanner.configure(),
        "watch_keywords": ["AI", "ai", "machine learning"],
    }
    initialized = _state(keyword_seen={KEY_AI: [], KEY_ML: []})
    with patch.object(scanner, "_api_get", return_value=_result([STORY])) as api:
        pollen, _ = scanner.poll(config, initialized)
    assert [item["id"] for item in pollen] == ["hn-story-40001"]
    assert api.call_count == 2


def test_mention_matches_exact_external_author_and_story_text():
    scanner = HackerNewsScanner()
    config = {**scanner.configure(), "username": "dhruvil"}
    hits = [
        MENTION,
        {**MENTION, "objectID": "40003", "comment_text": "dhruvilicious is here"},
        {**MENTION, "objectID": "40004", "author": "Dhruvil"},
        {
            **MENTION,
            "objectID": "40005",
            "comment_text": "",
            "story_text": "<p>Thanks, dhruvil!</p>",
        },
    ]
    with patch.object(scanner, "_api_get", return_value=_result(hits)):
        pollen, _ = scanner.poll(config, _state(username="dhruvil"))
    assert [item["id"] for item in pollen] == [
        "hn-mention-40002",
        "hn-mention-40005",
    ]
    assert pollen[1]["preview"] == "Thanks, dhruvil!"


def test_first_username_poll_silences_previous_day():
    scanner = HackerNewsScanner()
    config = {**scanner.configure(), "username": "dhruvil"}
    with patch.object(scanner, "_api_get", return_value=_result([MENTION])):
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    state = json.loads(watermark)
    assert state["mention_initialized"] is True
    assert state["mention_seen"] == ["40002"]
    assert state["created_epoch"] > 0


def test_mention_window_resumes_pages_without_advancing_time_early():
    scanner = HackerNewsScanner()
    config = {**scanner.configure(), "username": "dhruvil", "max_pages": 1}
    first = {**MENTION, "objectID": "41001"}
    second = {**MENTION, "objectID": "41002"}

    def page_zero(endpoint, params):
        assert params["page"] == 0
        return _result([first], pages=2)

    with patch.object(scanner, "_api_get", side_effect=page_zero):
        pollen, watermark = scanner.poll(config, _state(username="dhruvil"))
    assert [item["id"] for item in pollen] == ["hn-mention-41001"]
    mid = json.loads(watermark)
    assert mid["mention_page"] == 1
    assert mid["mention_pending"] == ["41001"]
    old_epoch = mid["created_epoch"]

    def page_one(endpoint, params):
        assert params["page"] == 1
        return _result([second], pages=2)

    with patch.object(scanner, "_api_get", side_effect=page_one):
        pollen, watermark = scanner.poll(config, watermark)
    assert [item["id"] for item in pollen] == ["hn-mention-41002"]
    final = json.loads(watermark)
    assert final["mention_page"] == 0
    assert final["mention_pending"] == []
    assert final["mention_seen"][-2:] == ["41001", "41002"]
    assert final["created_epoch"] >= old_epoch


def test_keyword_failure_preserves_that_component_while_mentions_progress():
    scanner = HackerNewsScanner()
    config = {
        **scanner.configure(),
        "watch_keywords": ["AI"],
        "username": "dhruvil",
    }
    original = _state(keyword_seen={KEY_AI: ["39999"]}, username="dhruvil")

    def fake(endpoint, params):
        if endpoint == "search":
            return None
        return _result([])

    with patch.object(scanner, "_api_get", side_effect=fake):
        pollen, watermark = scanner.poll(config, original)
    assert pollen == []
    assert json.loads(watermark)["keyword_seen"][KEY_AI] == ["39999"]


def test_invalid_configuration_preserves_watermark_without_requests():
    scanner = HackerNewsScanner()
    with patch.object(scanner, "_api_get") as api:
        assert scanner.poll({"watch_keywords": ["x"] * 21}, "safe") == ([], "safe")
        assert scanner.poll({"watch_keywords": [], "username": "x" * 101}, "safe") == ([], "safe")
        api.assert_not_called()


def test_keyword_budget_rotates_without_starving_later_keywords():
    scanner = HackerNewsScanner()
    config = {
        **scanner.configure(),
        "watch_keywords": ["AI", "machine learning", "Show HN"],
        "keywords_per_poll": 1,
    }
    state = _state(keyword_seen={KEY_AI: [], KEY_ML: [], KEY_SHOW: []})
    queries = []

    def fake(endpoint, params):
        queries.append(params["query"])
        return _result([])

    with patch.object(scanner, "_api_get", side_effect=fake):
        for _ in range(3):
            _, state = scanner.poll(config, state)

    assert queries == ["AI", "machine learning", "Show HN"]
    assert json.loads(state)["keyword_next_index"] == 0


def test_malformed_mention_page_does_not_advance_time_window():
    scanner = HackerNewsScanner()
    config = {**scanner.configure(), "username": "dhruvil"}
    state = _state(username="dhruvil")
    with patch.object(
        scanner,
        "_api_get",
        return_value={"hits": ["malformed"], "nbPages": 1},
    ):
        pollen, updated = scanner.poll(config, state)
    assert pollen == []
    assert json.loads(updated)["created_epoch"] == json.loads(state)["created_epoch"]


def test_keyword_search_honors_configured_page_budget():
    scanner = HackerNewsScanner()
    config = {
        **scanner.configure(),
        "watch_keywords": ["AI"],
        "max_items": 1,
        "max_pages": 2,
    }
    state = _state(keyword_seen={KEY_AI: []})
    calls = []

    def fake(endpoint, params):
        calls.append(params["page"])
        return _result([{**STORY, "objectID": str(40001 + params["page"])}], pages=2)

    with patch.object(scanner, "_api_get", side_effect=fake):
        pollen, _ = scanner.poll(config, state)
    assert calls == [0, 1]
    assert [item["id"] for item in pollen] == ["hn-story-40001", "hn-story-40002"]


def test_successful_mention_page_is_checkpointed_before_later_failure():
    scanner = HackerNewsScanner()
    config = {**scanner.configure(), "username": "dhruvil", "max_pages": 2}
    first = {**MENTION, "objectID": "41001"}

    def fake(endpoint, params):
        return _result([first], pages=2) if params["page"] == 0 else None

    with patch.object(scanner, "_api_get", side_effect=fake):
        pollen, watermark = scanner.poll(config, _state(username="dhruvil"))
    state = json.loads(watermark)
    assert [item["id"] for item in pollen] == ["hn-mention-41001"]
    assert state["mention_page"] == 1
    assert state["mention_pending"] == ["41001"]


def test_malformed_keyword_hit_and_current_state_fail_closed():
    scanner = HackerNewsScanner()
    config = {**scanner.configure(), "watch_keywords": ["AI"]}
    state = _state(keyword_seen={KEY_AI: ["39999"]})
    malformed = {**STORY, "objectID": "40002", "points": "250"}
    with patch.object(scanner, "_api_get", return_value=_result([STORY, malformed])):
        pollen, watermark = scanner.poll(config, state)
    assert pollen == []
    assert json.loads(watermark)["keyword_seen"][KEY_AI] == ["39999"]

    corrupt = json.loads(state)
    corrupt["mention_initialized"] = "true"
    corrupt_state = json.dumps(corrupt)
    with patch.object(scanner, "_api_get") as api:
        assert scanner.poll(config, corrupt_state) == ([], corrupt_state)
    api.assert_not_called()


def test_config_values_are_not_silently_coerced():
    scanner = HackerNewsScanner()
    for config in (
        {**scanner.configure(), "watch_keywords": "AI"},
        {**scanner.configure(), "watch_keywords": [" AI"]},
        {**scanner.configure(), "username": {"name": "dhruvil"}},
    ):
        with patch.object(scanner, "_api_get") as api:
            assert scanner.poll(config, "safe") == ([], "safe")
        api.assert_not_called()
