"""Functional tests for Notion's 2026 data-source and comment APIs."""

import importlib.util
import json
import os
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "notion_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "notion", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
NotionScanner = _mod.NotionScanner

PAGE_ID = "canonical-page-id"
PAGE = {
    "id": PAGE_ID,
    "last_edited_time": "2026-07-15T10:00:00.000Z",
    "last_edited_by": {"id": "user-xyz", "name": "Editor"},
    "url": "https://notion.so/canonical-page-id",
    "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "My Task"}]},
    },
}
COMMENT = {
    "id": "comment-456",
    "created_time": "2026-07-15T10:01:00.000Z",
    "created_by": {"id": "user-abc", "name": "Alice"},
    "rich_text": [{"plain_text": "Looks good to me"}],
}


def _config(scanner, **changes):
    return {**scanner.configure(), **changes}


def _state(*, comments=None):
    return json.dumps({
        "version": 3,
        "updated_at": "2026-07-15T09:00:00Z",
        "initialized": True,
        "comment_pages": comments or {},
    })


def test_defaults_use_current_data_source_api_and_rate_bounds():
    config = NotionScanner().configure()
    assert _mod.NOTION_VERSION == "2026-03-11"
    assert config["watch_data_sources"] == []
    assert config["watch_databases"] == []
    assert config["watch_comments"] is False
    assert config["max_items"] == 100
    assert config["max_pages"] == 3
    assert NotionScanner.MAX_WATCH_TARGETS == 5


def test_missing_or_invalid_token_env_preserves_watermark(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    assert scanner.poll(scanner.configure(), "safe") == ([], "safe")
    assert scanner.poll({"token_env": "BAD-NAME"}, "safe") == ([], "safe")


def test_first_poll_is_quiet_and_queries_after_scan_start(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_data_sources=["source-1"])
    bodies = []

    def fake(path, token, method="GET", body=None, params=None):
        bodies.append(body)
        return {"results": [PAGE], "has_more": False}

    with patch.object(scanner, "_api", side_effect=fake):
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    assert bodies[0]["filter"]["last_edited_time"]["after"] == json.loads(watermark)["updated_at"]


def test_data_source_page_update_has_edit_specific_identity(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_data_sources=["source-1"])
    with patch.object(
        scanner,
        "_api",
        return_value={"results": [PAGE], "has_more": False},
    ):
        pollen, _ = scanner.poll(config, _state())
    assert len(pollen) == 1
    item = pollen[0]
    assert item["type"] == "notion_page_updated"
    assert item["id"].startswith(f"notion-page-{PAGE_ID}-")
    assert item["title"] == "My Task"
    assert item["metadata"]["data_source_id"] == "source-1"


def test_database_ids_resolve_to_current_data_sources(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_databases=["database-1"])
    paths = []

    def fake(path, token, method="GET", body=None, params=None):
        paths.append((path, method))
        if path.startswith("/databases/"):
            return {"data_sources": [{"id": "source-1"}]}
        return {"results": [], "has_more": False}

    with patch.object(scanner, "_api", side_effect=fake):
        scanner.poll(config, _state())
    assert paths == [
        ("/databases/database-1", "GET"),
        ("/data_sources/source-1/query", "POST"),
    ]


def test_watched_page_update_and_integration_self_suppression(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_pages=["configured-page"])
    with patch.object(scanner, "_api", return_value=PAGE):
        pollen, _ = scanner.poll(config, _state())
    assert [item["type"] for item in pollen] == ["notion_page_updated"]

    self_config = {**config, "integration_user_id": "user-xyz"}
    with patch.object(scanner, "_api", return_value=PAGE):
        pollen, _ = scanner.poll(self_config, _state())
    assert pollen == []


def test_comment_boundary_handles_canonical_page_id_and_persists_it(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(
        scanner,
        watch_pages=["configured-page-without-hyphens"],
        watch_comments=True,
    )
    comments = {
        PAGE_ID: {
            "initialized": True,
            "last_created_time": "2026-07-15T10:00:00.000Z",
            "ids_at_last_time": ["old"],
            "missing_time_ids": [],
        }
    }

    def fake(path, token, method="GET", body=None, params=None):
        if path.startswith("/pages/"):
            return {**PAGE, "last_edited_time": "2026-07-15T08:00:00Z"}
        return {"results": [COMMENT], "has_more": False}

    with patch.object(scanner, "_api", side_effect=fake):
        pollen, watermark = scanner.poll(config, _state(comments=comments))
    assert [item["id"] for item in pollen] == ["notion-comment-comment-456"]
    assert pollen[0]["preview"] == "Looks good to me"
    assert PAGE_ID in json.loads(watermark)["comment_pages"]


def test_first_comment_poll_is_quiet_then_same_time_new_id_emits(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_pages=[PAGE_ID], watch_comments=True)

    def fake(path, token, method="GET", body=None, params=None):
        if path.startswith("/pages/"):
            return {**PAGE, "last_edited_time": "2026-07-15T08:00:00Z"}
        return {"results": [COMMENT], "has_more": False}

    with patch.object(scanner, "_api", side_effect=fake):
        pollen, watermark = scanner.poll(config, _state())
    assert pollen == []

    sibling = {**COMMENT, "id": "comment-sibling"}
    with patch.object(scanner, "_api", side_effect=lambda path, *args, **kwargs: (
        {**PAGE, "last_edited_time": "2026-07-15T08:00:00Z"}
        if path.startswith("/pages/")
        else {"results": [COMMENT, sibling], "has_more": False}
    )):
        pollen, _ = scanner.poll(config, watermark)
    assert [item["id"] for item in pollen] == ["notion-comment-comment-sibling"]


def test_pagination_overflow_and_api_error_preserve_watermark(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_data_sources=["source-1"], max_pages=1)
    watermark = _state()
    with patch.object(scanner, "_api", return_value={
        "results": [PAGE], "has_more": True, "next_cursor": "more",
    }):
        assert scanner.poll(config, watermark) == ([], watermark)
    with patch.object(scanner, "_api", return_value=None):
        assert scanner.poll(config, watermark) == ([], watermark)


def test_watch_target_limits_fail_before_network(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_data_sources=[f"s{i}" for i in range(26)])
    with patch.object(scanner, "_api") as api:
        assert scanner.poll(config, "safe") == ([], "safe")
        api.assert_not_called()

    combined = _config(
        scanner,
        watch_data_sources=["s1", "s2", "s3"],
        watch_pages=["p1", "p2", "p3"],
    )
    with patch.object(scanner, "_api") as api:
        assert scanner.poll(combined, "safe") == ([], "safe")
        api.assert_not_called()


def test_failed_target_does_not_rollback_successful_target(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_data_sources=["good", "bad"])
    state = json.dumps({
        "version": 4,
        "updated_at": "2026-07-15T09:00:00Z",
        "initialized": True,
        "target_updated_at": {
            "data_source:good": "2026-07-15T09:00:00Z",
            "data_source:bad": "2026-07-15T09:00:00Z",
        },
        "target_page_edits": {
            "data_source:good": {},
            "data_source:bad": {},
        },
        "comment_pages": {},
        "page_canonical_ids": {},
    }, sort_keys=True, separators=(",", ":"))

    def fake(path, token, method="GET", body=None, params=None):
        if "/data_sources/good/" in path:
            return {"results": [PAGE], "has_more": False}
        return None

    with patch.object(scanner, "_api", side_effect=fake):
        pollen, watermark = scanner.poll(config, state)

    assert [item["type"] for item in pollen] == ["notion_page_updated"]
    updated = json.loads(watermark)
    assert updated["target_updated_at"]["data_source:bad"] == (
        "2026-07-15T09:00:00Z"
    )
    assert updated["target_updated_at"]["data_source:good"] != (
        "2026-07-15T09:00:00Z"
    )


def test_overlap_does_not_reemit_unchanged_page_edit(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_data_sources=["source-1"])
    response = {"results": [PAGE], "has_more": False}

    with patch.object(scanner, "_api", return_value=response):
        first, watermark = scanner.poll(config, _state())
        second, _ = scanner.poll(config, watermark)

    assert [item["type"] for item in first] == ["notion_page_updated"]
    assert second == []


def test_pagination_flags_and_nested_page_fields_are_strict(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_data_sources=["source-1"])
    watermark = _state()
    for response in (
        {"results": [PAGE], "has_more": "false"},
        {"results": [PAGE]},
        {
            "results": [{**PAGE, "last_edited_by": {"id": {"bad": "shape"}}}],
            "has_more": False,
        },
    ):
        with patch.object(scanner, "_api", return_value=response):
            assert scanner.poll(config, watermark) == ([], watermark)


def test_comment_boundary_compares_real_instants_across_offsets(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_pages=[PAGE_ID], watch_comments=True)
    comments = {
        PAGE_ID: {
            "initialized": True,
            "last_created_time": "2026-07-15T10:00:00Z",
            "ids_at_last_time": ["old"],
            "missing_time_ids": [],
        }
    }
    offset_comment = {
        **COMMENT,
        "created_time": "2026-07-15T03:01:00-07:00",
    }

    def fake(path, token, method="GET", body=None, params=None):
        if path.startswith("/pages/"):
            return {**PAGE, "last_edited_time": "2026-07-15T08:00:00Z"}
        return {"results": [offset_comment], "has_more": False}

    with patch.object(scanner, "_api", side_effect=fake):
        pollen, _ = scanner.poll(config, _state(comments=comments))
    assert [item["id"] for item in pollen] == ["notion-comment-comment-456"]


def test_edit_history_compacts_instead_of_permanently_stalling(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_data_sources=["source-1"])
    edits = {
        f"old-{index}": "2026-07-15T08:00:00Z"
        for index in range(2000)
    }
    state = json.dumps({
        "version": 4,
        "updated_at": "2026-07-15T09:00:00Z",
        "initialized": True,
        "target_updated_at": {"data_source:source-1": "2026-07-15T09:00:00Z"},
        "target_page_edits": {"data_source:source-1": edits},
        "comment_pages": {},
        "page_canonical_ids": {},
    }, sort_keys=True, separators=(",", ":"))
    with patch.object(
        scanner,
        "_api",
        return_value={"results": [PAGE], "has_more": False},
    ):
        pollen, watermark = scanner.poll(config, state)
    saved = json.loads(watermark)["target_page_edits"]["data_source:source-1"]
    assert [item["type"] for item in pollen] == ["notion_page_updated"]
    assert len(saved) == 2000 and PAGE_ID in saved


def test_invalid_state_and_config_types_fail_before_network(monkeypatch):
    scanner = NotionScanner()
    monkeypatch.setenv("NOTION_TOKEN", "token")
    config = _config(scanner, watch_pages=[PAGE_ID])
    corrupt = json.loads(_state())
    corrupt["comment_pages"] = {PAGE_ID: {"initialized": "true"}}
    corrupt_state = json.dumps(corrupt)
    with patch.object(scanner, "_api") as api:
        assert scanner.poll(config, corrupt_state) == ([], corrupt_state)
    api.assert_not_called()

    for invalid in (
        _config(scanner, watch_pages=PAGE_ID),
        _config(scanner, watch_data_sources="source-1"),
        _config(scanner, integration_user_id={"id": "user-1"}),
        _config(scanner, token_env=123),
    ):
        with patch.object(scanner, "_api") as api:
            assert scanner.poll(invalid, "safe") == ([], "safe")
        api.assert_not_called()
