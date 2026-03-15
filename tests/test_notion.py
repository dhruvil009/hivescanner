"""Tests for Notion scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch

import pytest

# Load the notion adapter explicitly by file path to avoid collision
# with other community adapters that share the module name "adapter".
_adapter_path = os.path.join(os.path.dirname(__file__), "..", "community", "notion", "adapter.py")
_spec = importlib.util.spec_from_file_location("notion_adapter", _adapter_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
NotionScanner = _mod.NotionScanner

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}

SAMPLE_DB_PAGE = {
    "id": "page-abc-123",
    "last_edited_time": "2026-03-15T12:00:00Z",
    "last_edited_by": {"id": "user-xyz"},
    "url": "https://notion.so/page-abc-123",
    "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "My Task"}]},
    },
}

SAMPLE_COMMENT = {
    "id": "comment-456",
    "created_time": "2026-03-15T13:00:00Z",
    "created_by": {"id": "user-abc"},
    "rich_text": [{"plain_text": "Looks good to me"}],
}

SAMPLE_WATCHED_PAGE = {
    "id": "page-watched-1",
    "last_edited_time": "2026-03-15T14:00:00Z",
    "last_edited_by": {"id": "user-qrs"},
    "url": "https://notion.so/page-watched-1",
    "properties": {
        "Title": {"type": "title", "title": [{"plain_text": "Sprint Planning"}]},
    },
}


@pytest.fixture
def scanner():
    return NotionScanner()


class TestNotionScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["token_env"] == "NOTION_TOKEN"
        assert config["watch_databases"] == []
        assert config["watch_pages"] == []
        assert config["max_items"] == 20

    def test_poll_empty_when_no_token(self, scanner, monkeypatch):
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        pollen, wm = scanner.poll(
            {"token_env": "NOTION_TOKEN", "watch_databases": [], "watch_pages": []},
            "2026-03-15T09:00:00Z",
        )
        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"

    def test_database_page_emits_notion_page_updated(self, scanner, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "ntn_fake_token")

        def fake_api(path, token, method="GET", body=None):
            if "/databases/" in path and method == "POST":
                return {"results": [SAMPLE_DB_PAGE]}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {
                    "token_env": "NOTION_TOKEN",
                    "watch_databases": ["db-001"],
                    "watch_pages": [],
                    "max_items": 20,
                },
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "notion_page_updated"
        assert p["source"] == "notion"
        assert p["title"] == "My Task"
        assert p["author"] == "user-xyz"
        assert p["id"] == "notion-page-page-abc-123"
        assert p["group"] == "db-db-001"
        assert p["url"] == "https://notion.so/page-abc-123"

    def test_watched_page_update_emits_notion_page_updated(self, scanner, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "ntn_fake_token")

        def fake_api(path, token, method="GET", body=None):
            if path.startswith("/pages/"):
                return SAMPLE_WATCHED_PAGE
            if path.startswith("/comments"):
                return {"results": []}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {
                    "token_env": "NOTION_TOKEN",
                    "watch_databases": [],
                    "watch_pages": ["page-watched-1"],
                    "max_items": 20,
                },
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "notion_page_updated"
        assert p["source"] == "notion"
        assert p["title"] == "Sprint Planning"
        assert p["author"] == "user-qrs"
        assert p["group"] == "pages"

    def test_comment_emits_notion_comment(self, scanner, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "ntn_fake_token")

        # Page last_edited_time <= watermark so no page-update pollen, only comment
        old_page = {**SAMPLE_WATCHED_PAGE, "last_edited_time": "2026-03-15T09:00:00Z"}

        def fake_api(path, token, method="GET", body=None):
            if path.startswith("/pages/"):
                return old_page
            if path.startswith("/comments"):
                return {"results": [SAMPLE_COMMENT]}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {
                    "token_env": "NOTION_TOKEN",
                    "watch_databases": [],
                    "watch_pages": ["page-watched-1"],
                    "max_items": 20,
                },
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "notion_comment"
        assert p["source"] == "notion"
        assert p["id"] == "notion-comment-comment-456"
        assert p["title"] == "Looks good to me"
        assert p["preview"] == "Looks good to me"
        assert p["author"] == "user-abc"

    def test_watermark_filters_old_comments(self, scanner, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "ntn_fake_token")

        old_comment = {**SAMPLE_COMMENT, "created_time": "2026-03-15T08:00:00Z"}
        old_page = {**SAMPLE_WATCHED_PAGE, "last_edited_time": "2026-03-15T08:00:00Z"}

        def fake_api(path, token, method="GET", body=None):
            if path.startswith("/pages/"):
                return old_page
            if path.startswith("/comments"):
                return {"results": [old_comment]}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {
                    "token_env": "NOTION_TOKEN",
                    "watch_databases": [],
                    "watch_pages": ["page-watched-1"],
                    "max_items": 20,
                },
                "2026-03-15T09:00:00Z",
            )

        assert pollen == []

    def test_pollen_schema_has_all_required_keys(self, scanner, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "ntn_fake_token")

        def fake_api(path, token, method="GET", body=None):
            if "/databases/" in path and method == "POST":
                return {"results": [SAMPLE_DB_PAGE]}
            return None

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {
                    "token_env": "NOTION_TOKEN",
                    "watch_databases": ["db-001"],
                    "watch_pages": [],
                    "max_items": 20,
                },
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert set(pollen[0].keys()) >= REQUIRED_POLLEN_KEYS

    def test_api_error_preserves_watermark(self, scanner, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "ntn_fake_token")

        def fake_api(path, token, method="GET", body=None):
            return None  # simulate all API calls failing

        with patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                {
                    "token_env": "NOTION_TOKEN",
                    "watch_databases": ["db-001"],
                    "watch_pages": ["page-watched-1"],
                    "max_items": 20,
                },
                "2026-03-15T09:00:00Z",
            )

        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"
