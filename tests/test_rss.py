"""Functional and SSRF tests for the RSS/Atom scanner."""

import hashlib
import importlib.util
import json
import pathlib
import socket
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "rss_adapter", ROOT / "community" / "rss" / "adapter.py"
)
rss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rss)
RssScanner = rss.RssScanner

FEED = "https://example.com/feed.xml"
RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
  <item><guid>old-guid</guid><title>Old Post</title><link>/old</link>
    <pubDate>Tue, 14 Jul 2026 10:00:00 GMT</pubDate></item>
  <item><guid>new-guid</guid><title>New Release</title><link>/new</link>
    <pubDate>Tue, 14 Jul 2026 12:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM_XML = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>
  <entry><id>atom-1</id><title>Atom Entry</title>
    <link rel="alternate" href="/atom1"/><published>2026-07-14T14:00:00Z</published>
  </entry>
</feed>"""


def _fingerprint(identity):
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def _state(*, published="2026-07-14T09:00:00+00:00", ids=None, undated=None):
    key = hashlib.sha256(FEED.encode()).hexdigest()[:16]
    return json.dumps({
        "version": 3,
        "feeds": {key: {
            "initialized": True,
            "max_published": published,
            "ids_at_max_published": ids or [],
            "undated_seen": undated or [],
        }},
    }, sort_keys=True, separators=(",", ":"))


def test_date_parser_handles_rfc2822_and_iso_offsets():
    assert rss._parse_date("Tue, 14 Jul 2026 10:00:00 GMT") == datetime(
        2026, 7, 14, 10, tzinfo=timezone.utc
    )
    assert rss._parse_date("2026-07-14T10:00:00Z") == datetime(
        2026, 7, 14, 10, tzinfo=timezone.utc
    )
    assert rss._parse_date("garbage") is None


def test_defaults_bound_feeds_entries_and_bytes():
    config = RssScanner().configure()
    assert config == {
        "enabled": False,
        "feeds": [],
        "max_items_per_feed": 20,
        "feeds_per_poll": 2,
    }
    assert rss.MAX_FEED_BYTES == 1_000_000
    assert rss.MAX_REDIRECTS == 3
    assert rss.MAX_FETCH_SECONDS == 20


def test_private_or_mixed_dns_answers_are_rejected():
    private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    with patch.object(socket, "getaddrinfo", return_value=private):
        with pytest.raises(ValueError, match="non-public"):
            rss._public_addresses("example.com", 443)

    mixed = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
    ]
    with patch.object(socket, "getaddrinfo", return_value=mixed):
        with pytest.raises(ValueError, match="non-public"):
            rss._public_addresses("example.com", 443)


def test_first_feed_poll_is_quiet_and_builds_exact_boundary():
    scanner = RssScanner()
    with patch.object(rss, "_fetch_public_url", return_value=(RSS_XML, FEED)):
        pollen, watermark = scanner.poll({"feeds": [FEED]}, "")
    assert pollen == []
    state = json.loads(watermark)
    feed_state = next(iter(state["feeds"].values()))
    assert feed_state["max_published"] == "2026-07-14T12:00:00+00:00"
    assert feed_state["ids_at_max_published"] == [_fingerprint("new-guid")]


def test_rss_and_atom_entries_emit_with_relative_links_resolved():
    scanner = RssScanner()
    with patch.object(rss, "_fetch_public_url", return_value=(RSS_XML, FEED)):
        pollen, _ = scanner.poll({"feeds": [FEED]}, _state())
    assert [item["title"] for item in pollen] == ["Old Post", "New Release"]
    assert [item["url"] for item in pollen] == [
        "https://example.com/old", "https://example.com/new",
    ]
    assert all(item["id"].startswith("rss-") for item in pollen)

    with patch.object(rss, "_fetch_public_url", return_value=(ATOM_XML, FEED)):
        pollen, _ = scanner.poll({"feeds": [FEED]}, _state())
    assert [item["title"] for item in pollen] == ["Atom Entry"]
    assert pollen[0]["url"] == "https://example.com/atom1"


def test_per_feed_cap_drains_oldest_first_without_skipping():
    scanner = RssScanner()
    config = {"feeds": [FEED], "max_items_per_feed": 1}
    with patch.object(rss, "_fetch_public_url", return_value=(RSS_XML, FEED)):
        first, watermark = scanner.poll(config, _state())
        second, _ = scanner.poll(config, watermark)
    assert [item["title"] for item in first] == ["Old Post"]
    assert [item["title"] for item in second] == ["New Release"]


def test_same_timestamp_ids_and_undated_ids_are_exactly_deduplicated():
    xml = b"""<rss><channel>
      <item><guid>a</guid><title>A</title><pubDate>Tue, 14 Jul 2026 10:00:00 GMT</pubDate></item>
      <item><guid>b</guid><title>B</title><pubDate>Tue, 14 Jul 2026 10:00:00 GMT</pubDate></item>
      <item><guid>u</guid><title>Undated</title></item>
    </channel></rss>"""
    scanner = RssScanner()
    state = _state(
        published="2026-07-14T10:00:00+00:00",
        ids=[_fingerprint("a")],
        undated=[_fingerprint("u")],
    )
    with patch.object(rss, "_fetch_public_url", return_value=(xml, FEED)):
        pollen, _ = scanner.poll({"feeds": [FEED]}, state)
    assert [item["title"] for item in pollen] == ["B"]


def test_dtd_and_entity_declarations_are_rejected():
    scanner = RssScanner()
    malicious = b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel/></rss>'
    with patch.object(rss, "_fetch_public_url", return_value=(malicious, FEED)):
        pollen, watermark = scanner.poll({"feeds": [FEED]}, _state())
    assert pollen == []
    assert watermark == _state()


def test_one_feed_failure_does_not_discard_another_feeds_progress():
    scanner = RssScanner()
    broken = "https://broken.example/feed"

    def fake(url):
        if url == broken:
            raise OSError("connection failed")
        return RSS_XML, FEED

    with patch.object(rss, "_fetch_public_url", side_effect=fake):
        pollen, watermark = scanner.poll(
            {"feeds": [broken, FEED]}, _state()
        )
    assert len(pollen) == 2
    assert hashlib.sha256(FEED.encode()).hexdigest()[:16] in json.loads(watermark)["feeds"]


def test_invalid_feed_lists_fail_closed_before_fetch():
    scanner = RssScanner()
    with patch.object(rss, "_fetch_public_url") as fetch:
        assert scanner.poll({"feeds": ["x"] * 26}, "safe") == ([], "safe")
        assert scanner.poll({"feeds": ["x" * 2049]}, "safe") == ([], "safe")
        assert scanner.poll({"feeds": ["https://example.com/feed\nHost: evil"]}, "safe") == ([], "safe")
        assert scanner.poll({"feeds": [" https://example.com/feed"]}, "safe") == ([], "safe")
        fetch.assert_not_called()


def test_uncommittable_boundary_drops_emissions_instead_of_replaying_them():
    scanner = RssScanner()
    published = "2026-07-14T10:00:00+00:00"
    committed_ids = [_fingerprint(f"old-{index}") for index in range(100)]
    state = _state(published=published, ids=committed_ids)
    xml = b"""<rss><channel><item><guid>new</guid><title>New</title>
      <pubDate>Tue, 14 Jul 2026 10:00:00 GMT</pubDate>
    </item></channel></rss>"""

    with patch.object(rss, "_fetch_public_url", return_value=(xml, FEED)):
        pollen, watermark = scanner.poll({"feeds": [FEED]}, state)

    assert pollen == []
    assert watermark == state


def test_feed_rotation_bounds_fetches_and_advances_across_polls():
    scanner = RssScanner()
    feeds = [f"https://example.com/feed-{index}.xml" for index in range(5)]
    fetched = []

    def fake(url):
        fetched.append(url)
        return RSS_XML, url

    with patch.object(rss, "_fetch_public_url", side_effect=fake):
        _, first_state = scanner.poll({"feeds": feeds, "feeds_per_poll": 2}, "")
        assert fetched == feeds[:2]
        fetched.clear()
        scanner.poll({"feeds": feeds, "feeds_per_poll": 2}, first_state)
    assert fetched == feeds[2:4]


def test_undated_entry_that_later_gets_a_date_is_not_emitted_twice():
    scanner = RssScanner()
    undated_xml = b"""<rss><channel>
      <item><guid>same</guid><title>Entry</title></item>
    </channel></rss>"""
    dated_xml = b"""<rss><channel>
      <item><guid>same</guid><title>Entry</title>
        <pubDate>Tue, 14 Jul 2026 12:00:00 GMT</pubDate></item>
    </channel></rss>"""
    with patch.object(rss, "_fetch_public_url", return_value=(undated_xml, FEED)):
        _, state = scanner.poll({"feeds": [FEED]}, "")
    with patch.object(rss, "_fetch_public_url", return_value=(dated_xml, FEED)):
        pollen, _ = scanner.poll({"feeds": [FEED]}, state)
    assert pollen == []


def test_namespaced_rss_one_entries_are_discovered():
    scanner = RssScanner()
    rss_one = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns:rss="http://purl.org/rss/1.0/">
      <rss:item><rss:title>Namespaced post</rss:title><rss:link>/post</rss:link>
        <rss:date>2026-07-14T12:00:00Z</rss:date><rss:id>rss-one</rss:id>
      </rss:item></rdf:RDF>"""
    with patch.object(rss, "_fetch_public_url", return_value=(rss_one, FEED)):
        pollen, _ = scanner.poll({"feeds": [FEED]}, _state())
    assert [item["title"] for item in pollen] == ["Namespaced post"]
    assert pollen[0]["url"] == "https://example.com/post"


def test_unsafe_entry_link_is_not_forwarded():
    scanner = RssScanner()
    xml = b"""<rss><channel><item><guid>unsafe</guid><title>Unsafe</title>
      <link>javascript:alert(1)</link><pubDate>Tue, 14 Jul 2026 12:00:00 GMT</pubDate>
    </item></channel></rss>"""
    with patch.object(rss, "_fetch_public_url", return_value=(xml, FEED)):
        pollen, _ = scanner.poll({"feeds": [FEED]}, _state())
    assert pollen[0]["url"] == ""


def test_invalid_current_state_and_singleton_feed_config_fail_closed():
    scanner = RssScanner()
    corrupt = json.loads(_state())
    key = next(iter(corrupt["feeds"]))
    corrupt["feeds"][key]["initialized"] = "true"
    corrupt_state = json.dumps(corrupt)
    with patch.object(rss, "_fetch_public_url") as fetch:
        assert scanner.poll({"feeds": [FEED]}, corrupt_state) == ([], corrupt_state)
        assert scanner.poll({"feeds": FEED}, "safe") == ([], "safe")
    fetch.assert_not_called()
