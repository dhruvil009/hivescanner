"""Tests for RSS scanner."""

import hashlib
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "community", "rss"))
from adapter import RssScanner

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}

SAMPLE_RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>New Release v2.0</title>
      <link>https://example.com/v2</link>
      <pubDate>2026-03-15T12:00:00Z</pubDate>
    </item>
    <item>
      <title>Old Post</title>
      <link>https://example.com/old</link>
      <pubDate>2026-03-14T10:00:00Z</pubDate>
    </item>
  </channel>
</rss>"""

SAMPLE_ATOM_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
  <entry>
    <title>Atom Entry</title>
    <link href="https://example.com/atom1"/>
    <published>2026-03-15T14:00:00Z</published>
  </entry>
</feed>"""


def _make_urlopen_response(data: bytes):
    """Create a mock urlopen response context manager."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.__enter__ = lambda self: self
    mock_resp.__exit__ = lambda self, *a: None
    return mock_resp


@pytest.fixture
def scanner():
    return RssScanner()


class TestRssScanner:

    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["feeds"] == []
        assert config["max_items_per_feed"] == 5

    def test_poll_empty_when_no_feeds(self, scanner):
        pollen, wm = scanner.poll({"feeds": [], "max_items_per_feed": 5}, "")
        assert pollen == []

    def test_rss_feed_emits_rss_item(self, scanner):
        mock_resp = _make_urlopen_response(SAMPLE_RSS_XML)
        feed_url = "https://example.com/feed.xml"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            config = {"feeds": [feed_url], "max_items_per_feed": 10}
            pollen, wm = scanner.poll(config, "")

        assert len(pollen) == 2
        assert pollen[0]["type"] == "rss_item"
        assert pollen[0]["source"] == "rss"
        assert pollen[0]["title"] == "New Release v2.0"
        assert pollen[0]["url"] == "https://example.com/v2"

        expected_id = f"rss-{hashlib.sha256(f'{feed_url}:New Release v2.0'.encode()).hexdigest()[:12]}"
        assert pollen[0]["id"] == expected_id

    def test_watermark_filters_old_items(self, scanner):
        mock_resp = _make_urlopen_response(SAMPLE_RSS_XML)
        feed_url = "https://example.com/feed.xml"
        # Watermark between the two items' pubDates
        watermark = "2026-03-14T10:00:00Z"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            config = {"feeds": [feed_url], "max_items_per_feed": 10}
            pollen, wm = scanner.poll(config, watermark)

        # Old Post has pubDate == watermark, so it should be skipped (<=)
        assert len(pollen) == 1
        assert pollen[0]["title"] == "New Release v2.0"

    def test_pollen_schema_has_all_required_keys(self, scanner):
        mock_resp = _make_urlopen_response(SAMPLE_RSS_XML)
        feed_url = "https://example.com/feed.xml"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            config = {"feeds": [feed_url], "max_items_per_feed": 10}
            pollen, wm = scanner.poll(config, "")

        assert len(pollen) > 0, "Expected at least one pollen item"
        for item in pollen:
            missing = REQUIRED_POLLEN_KEYS - set(item.keys())
            assert not missing, f"Pollen missing keys: {missing}"

    def test_fetch_error_preserves_watermark(self, scanner):
        feed_url = "https://example.com/broken.xml"
        original_watermark = "2026-03-14T00:00:00Z"

        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            config = {"feeds": [feed_url], "max_items_per_feed": 5}
            pollen, wm = scanner.poll(config, original_watermark)

        assert pollen == []
        assert wm == original_watermark

    def test_atom_feed_emits_rss_item(self, scanner):
        mock_resp = _make_urlopen_response(SAMPLE_ATOM_XML)
        feed_url = "https://example.com/atom.xml"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            config = {"feeds": [feed_url], "max_items_per_feed": 10}
            pollen, wm = scanner.poll(config, "")

        assert len(pollen) == 1
        assert pollen[0]["type"] == "rss_item"
        assert pollen[0]["title"] == "Atom Entry"
        assert pollen[0]["url"] == "https://example.com/atom1"

    def test_max_items_per_feed_limits_output(self, scanner):
        mock_resp = _make_urlopen_response(SAMPLE_RSS_XML)
        feed_url = "https://example.com/feed.xml"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            config = {"feeds": [feed_url], "max_items_per_feed": 1}
            pollen, wm = scanner.poll(config, "")

        assert len(pollen) == 1

    def test_metadata_contains_feed_url(self, scanner):
        mock_resp = _make_urlopen_response(SAMPLE_RSS_XML)
        feed_url = "https://example.com/feed.xml"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            config = {"feeds": [feed_url], "max_items_per_feed": 10}
            pollen, wm = scanner.poll(config, "")

        for item in pollen:
            assert item["metadata"]["feed_url"] == feed_url
