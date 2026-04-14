"""Tests for RSS scanner."""

import hashlib
import importlib.util
import pathlib
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "rss_adapter", ROOT / "community" / "rss" / "adapter.py"
)
rss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rss)
RssScanner = rss.RssScanner

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
      <pubDate>Sun, 15 Mar 2026 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Old Post</title>
      <link>https://example.com/old</link>
      <pubDate>Sat, 14 Mar 2026 10:00:00 GMT</pubDate>
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

INTEGRATION_RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Old Item</title>
      <link>https://example.com/old</link>
      <pubDate>Sat, 10 Apr 2026 10:00:00 GMT</pubDate>
    </item>
    <item>
      <title>New Item</title>
      <link>https://example.com/new</link>
      <pubDate>Sun, 13 Apr 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""


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


class TestParseDate:

    def test_rfc2822_parses_to_utc(self):
        dt = rss._parse_date("Sat, 12 Apr 2026 10:00:00 GMT")
        assert dt == datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)

    def test_iso8601_with_z_parses_to_utc(self):
        dt = rss._parse_date("2026-04-13T00:00:00Z")
        assert dt == datetime(2026, 4, 13, 0, 0, 0, tzinfo=timezone.utc)

    def test_iso8601_with_offset_parses(self):
        dt = rss._parse_date("2026-04-13T00:00:00+00:00")
        assert dt is not None
        assert dt == datetime(2026, 4, 13, 0, 0, 0, tzinfo=timezone.utc)

    def test_empty_returns_none(self):
        assert rss._parse_date("") is None

    def test_garbage_returns_none(self):
        assert rss._parse_date("garbage") is None

    def test_rfc2822_compares_less_than_iso8601(self):
        rfc = rss._parse_date("Sat, 12 Apr 2026 10:00:00 GMT")
        iso = rss._parse_date("2026-04-13T00:00:00Z")
        assert rfc < iso


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

    def test_watermark_filters_old_rss_items(self, scanner):
        """Integration: RFC 2822 pubDate vs ISO 8601 watermark filters correctly."""
        mock_resp = _make_urlopen_response(INTEGRATION_RSS_XML)
        feed_url = "https://example.com/feed.xml"
        watermark = "2026-04-12T00:00:00Z"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            config = {"feeds": [feed_url], "max_items_per_feed": 10}
            pollen, wm = scanner.poll(config, watermark)

        assert len(pollen) == 1
        assert pollen[0]["title"] == "New Item"
        assert pollen[0]["url"] == "https://example.com/new"

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
