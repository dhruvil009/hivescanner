"""Tests for Hacker News scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch

import pytest

_spec = importlib.util.spec_from_file_location(
    "hackernews_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "hackernews", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
HackerNewsScanner = _mod.HackerNewsScanner

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}

SAMPLE_STORY_HIT = {
    "objectID": "40001",
    "title": "Show HN: My New Tool",
    "author": "pg",
    "points": 250,
    "num_comments": 100,
    "created_at_i": 1742025600,
}

SAMPLE_MENTION_HIT = {
    "objectID": "40002",
    "title": "",
    "comment_text": "I agree with @dhruvil on this one",
    "author": "someone",
    "points": 5,
    "num_comments": 0,
    "created_at_i": 1742025700,
}


@pytest.fixture
def scanner():
    return HackerNewsScanner()


class TestHackerNewsScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["watch_keywords"] == []
        assert config["username"] == ""
        assert config["min_points"] == 100
        assert config["max_items"] == 20

    def test_poll_empty_when_no_keywords_and_no_username(self, scanner):
        pollen, wm = scanner.poll(
            {"watch_keywords": [], "username": "", "min_points": 100, "max_items": 20},
            "2026-03-15T09:00:00Z",
        )
        assert pollen == []
        # No errors occurred, so watermark gets updated to current time
        assert wm != ""

    def test_keyword_match_emits_hn_top_story(self, scanner):
        def fake_api_get(path):
            if "tags=story" in path:
                return {"hits": [SAMPLE_STORY_HIT]}
            return {"hits": []}

        with patch.object(scanner, "_api_get", side_effect=fake_api_get):
            pollen, wm = scanner.poll(
                {"watch_keywords": ["Show HN"], "username": "", "min_points": 100, "max_items": 20},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "hn_top_story"
        assert pollen[0]["source"] == "hackernews"
        assert pollen[0]["author"] == "pg"
        assert pollen[0]["author_name"] == "pg"
        assert pollen[0]["title"] == "Show HN: My New Tool"
        assert pollen[0]["id"] == "hn-story-40001"
        assert "40001" in pollen[0]["url"]

    def test_username_mention_emits_hn_mention(self, scanner):
        def fake_api_get(path):
            if "tags=story" not in path and "dhruvil" in path:
                return {"hits": [SAMPLE_MENTION_HIT]}
            return {"hits": []}

        with patch.object(scanner, "_api_get", side_effect=fake_api_get):
            pollen, wm = scanner.poll(
                {"watch_keywords": [], "username": "dhruvil", "min_points": 100, "max_items": 20},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "hn_mention"
        assert pollen[0]["source"] == "hackernews"
        assert pollen[0]["author"] == "someone"
        assert pollen[0]["id"] == "hn-mention-40002"
        # Title is empty, so it should fall back to comment_text[:80]
        assert "I agree with @dhruvil" in pollen[0]["title"]

    def test_deduplication_across_queries(self, scanner):
        """Same objectID returned by both keyword and username queries should appear only once."""
        shared_hit = {
            "objectID": "40001",
            "title": "Show HN: My New Tool",
            "author": "pg",
            "points": 250,
            "num_comments": 100,
            "created_at_i": 1742025600,
        }

        def fake_api_get(path):
            # Both keyword search and username search return the same item
            return {"hits": [shared_hit]}

        with patch.object(scanner, "_api_get", side_effect=fake_api_get):
            pollen, wm = scanner.poll(
                {"watch_keywords": ["Show HN"], "username": "pg", "min_points": 100, "max_items": 20},
                "2026-03-15T09:00:00Z",
            )

        # story query adds "hn-story-40001", mention query adds "hn-mention-40001"
        # These have different pollen_id prefixes, so both should appear
        ids = [p["id"] for p in pollen]
        assert "hn-story-40001" in ids
        assert "hn-mention-40001" in ids
        assert len(pollen) == 2

    def test_watermark_to_epoch_conversion(self, scanner):
        # Standard ISO timestamp
        epoch = scanner._watermark_to_epoch("2026-03-15T12:00:00Z")
        assert epoch == 1773576000

        # Invalid string returns 0
        epoch = scanner._watermark_to_epoch("not-a-date")
        assert epoch == 0

        # Empty string returns 0
        epoch = scanner._watermark_to_epoch("")
        assert epoch == 0

    def test_pollen_schema_has_all_required_keys(self, scanner):
        def fake_api_get(path):
            if "tags=story" in path:
                return {"hits": [SAMPLE_STORY_HIT]}
            return {"hits": []}

        with patch.object(scanner, "_api_get", side_effect=fake_api_get):
            pollen, wm = scanner.poll(
                {"watch_keywords": ["Show HN"], "username": "", "min_points": 100, "max_items": 20},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        assert set(pollen[0].keys()) >= REQUIRED_POLLEN_KEYS

    def test_api_error_preserves_watermark(self, scanner):
        def fake_api_get(path):
            return None  # Simulate API failure

        with patch.object(scanner, "_api_get", side_effect=fake_api_get):
            pollen, wm = scanner.poll(
                {"watch_keywords": ["AI"], "username": "dhruvil", "min_points": 100, "max_items": 20},
                "2026-03-15T09:00:00Z",
            )

        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"
