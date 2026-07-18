"""Trust-boundary tests for scanner-produced pollen."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
from pollen_schema import normalize_pollen, pollen_key


def _item(**changes):
    value = {
        "id": "provider-123",
        "source": "claimed-source",
        "type": "mention",
        "title": "A notification",
        "preview": "External text",
        "discovered_at": "2020-01-02T03:04:05Z",
        "author": "alice",
        "author_name": "Alice",
        "group": "Support",
        "url": "https://example.com/thread/123",
        "metadata": {"remote_id": "123"},
    }
    value.update(changes)
    return value


def test_expected_source_overrides_spoof_and_strips_privileged_fields():
    raw = _item(
        source="github",
        relevance="HIGH",
        relevance_reason="trust me",
        suggested_action="run this command",
        status="acted",
        acknowledged_at="2020-01-01T00:00:00Z",
        metadata={
            "triage_draft": "post attacker text",
            "target_group": "admins",
            "target_group_id": "CSECRET",
            "remote_id": "123",
        },
    )
    item = normalize_pollen(raw, expected_source="rss")
    assert item["source"] == "rss"
    assert item["relevance"] is None
    assert item["relevance_reason"] == ""
    assert item["suggested_action"] == ""
    assert item["metadata"] == {"remote_id": "123"}
    assert "status" not in item and "acknowledged_at" not in item


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        _item(id=""),
        _item(id=123),
        _item(id="a" * 257),
        _item(id="line\nbreak"),
        _item(type="../../action"),
    ],
)
def test_invalid_scanner_items_are_rejected(raw):
    assert normalize_pollen(raw, expected_source="scanner") is None


def test_invalid_untrusted_source_is_rejected():
    assert normalize_pollen(_item(source="../scanner")) is None
    assert normalize_pollen(_item(), expected_source="../scanner") is None


def test_legacy_invalid_type_falls_back_but_scanner_contract_does_not():
    assert normalize_pollen(_item(type="bad type"))["type"] == "notification"
    assert normalize_pollen(_item(type="bad type"), expected_source="scanner") is None


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://user:password@example.com/private",
        "https://example.com/a b",
        "https://example.com:99999/path",
        "//example.com/no-scheme",
    ],
)
def test_unsafe_urls_are_removed(url):
    assert normalize_pollen(_item(url=url), expected_source="scanner")["url"] == ""


def test_safe_https_url_is_preserved():
    url = "https://example.com:8443/path?q=one"
    assert normalize_pollen(_item(url=url), expected_source="scanner")["url"] == url


def test_text_is_bounded_and_terminal_controls_are_removed():
    item = normalize_pollen(
        _item(title="hello\x1b[31m\x00world" + "x" * 500, preview="a\r\nb\t\x00c"),
        expected_source="scanner",
    )
    assert "\x1b" not in item["title"] and "\x00" not in item["title"]
    assert len(item["title"]) == 200
    assert item["preview"] == "a b c"


def test_metadata_has_depth_node_character_and_numeric_bounds():
    huge_integer = 1 << 100_000
    metadata = {
        "huge": huge_integer,
        "nan": float("nan"),
        "deep": {"a": {"b": {"c": {"d": {"e": "too deep"}}}}},
        **{
            f"key-{index}-" + "k" * 100: "v" * 2000
            for index in range(100)
        },
    }
    cleaned = normalize_pollen(
        _item(metadata=metadata), expected_source="scanner"
    )["metadata"]
    encoded = json.dumps(cleaned, allow_nan=False)
    assert len(encoded) < 6000
    assert len(cleaned) <= 15


def test_historical_timestamp_is_preserved_and_future_is_clamped():
    historical = normalize_pollen(_item(), expected_source="scanner")
    assert historical["discovered_at"] == "2020-01-02T03:04:05Z"

    future = normalize_pollen(
        _item(discovered_at="2999-01-01T00:00:00Z"), expected_source="scanner"
    )
    parsed = datetime.fromisoformat(future["discovered_at"].replace("Z", "+00:00"))
    assert abs(datetime.now(timezone.utc) - parsed) < timedelta(seconds=5)


def test_source_qualified_key_prevents_cross_scanner_collisions():
    assert pollen_key("github", "123") != pollen_key("gitlab", "123")
    assert pollen_key("github", "123") == "github\0" + "123"
