"""Functional tests for Gmail-backed package tracking."""

import base64
import hashlib
import importlib.util
import json
import os
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "package_tracking_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "package-tracking", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
PackageTrackingScanner = _mod.PackageTrackingScanner


def _message(msg_id, subject="Your order has shipped!", body=None):
    body = body or "Your UPS package 1ZABCDEF1234567890 has shipped."
    encoded = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": msg_id,
        "internalDate": "1786000001000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "Amazon.com <ship@amazon.com>"},
            ],
            "mimeType": "text/plain",
            "body": {"data": encoded},
        },
    }


def _state(config, *, seen=None, boundary=1_786_000_000_000):
    scope = hashlib.sha256(config["search_query"].encode()).hexdigest()[:16]
    return json.dumps({
        "version": 3,
        "initialized": True,
        "scope": scope,
        "internal_date_ms": boundary,
        "seen_ids": seen or [],
    })


def _api(stub_ids, messages, calls=None):
    def fake(path, token):
        if calls is not None:
            calls.append(path)
        if path.startswith("messages?"):
            return {"messages": [{"id": value} for value in stub_ids]}
        msg_id = path.split("/", 1)[1].split("?", 1)[0]
        return messages[msg_id]

    return fake


def test_defaults_bound_gmail_search_and_detail_work():
    config = PackageTrackingScanner().configure()
    assert config["token_env"] == "GOOGLE_ACCESS_TOKEN"
    assert config["max_items"] == 20
    assert config["max_pages"] == 5
    assert config["overlap_seconds"] == 300
    assert "delivery" in config["search_query"]


def test_tracking_number_extractors_are_carrier_aware():
    scanner = PackageTrackingScanner()
    assert scanner._extract_tracking_number("1ZABCDEF1234567890") == (
        "1ZABCDEF1234567890", "UPS",
    )
    assert scanner._extract_tracking_number("9400111899223100012345") == (
        "9400111899223100012345", "USPS",
    )
    assert scanner._extract_tracking_number("FedEx 123456789012") == (
        "123456789012", "FedEx",
    )
    assert scanner._extract_tracking_number("invoice 123456789012") == ("", "")


def test_event_detection_avoids_negative_delivery_and_shipping_phrases():
    scanner = PackageTrackingScanner()
    assert scanner._detect_event_type("Package is out for delivery") == "package_out_for_delivery"
    assert scanner._detect_event_type("Package is not out for delivery") == "package_update"
    assert scanner._detect_event_type("Package delivered") == "package_delivered"
    assert scanner._detect_event_type("Package was not delivered") == "package_update"
    assert scanner._detect_event_type("Order has not shipped") == "package_update"
    assert scanner._detect_event_type("Your order will be shipped tomorrow") == "package_update"
    assert scanner._detect_event_type("Expected to be delivered Friday") == "package_update"
    assert scanner._detect_event_type("Scheduled to be out for delivery tomorrow") == "package_update"
    assert scanner._detect_event_type("Unrecognized status") == "package_update"


def test_mime_tree_prefers_plain_text_and_sanitizes_html_fallback():
    scanner = PackageTrackingScanner()
    plain = base64.urlsafe_b64encode(b"Plain tracking body").decode()
    html = base64.urlsafe_b64encode(
        b"<style>bad</style><p>HTML body</p><script>ignore()</script>"
    ).decode()
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": html}},
            {"mimeType": "text/plain", "body": {"data": plain}},
        ],
    }
    assert scanner._decode_body(payload) == "Plain tracking body"
    assert scanner._decode_body({"mimeType": "text/html", "body": {"data": html}}) == "HTML body"


def test_missing_token_and_invalid_query_preserve_watermark(monkeypatch):
    scanner = PackageTrackingScanner()
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)
    assert scanner.poll(scanner.configure(), "safe") == ([], "safe")
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "token")
    assert scanner.poll({**scanner.configure(), "search_query": ""}, "safe") == ([], "safe")


def test_first_enable_lists_one_page_quietly_without_full_fetch(monkeypatch):
    scanner = PackageTrackingScanner()
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "token")
    config = scanner.configure()
    calls = []
    with patch.object(
        scanner,
        "_gmail_api",
        side_effect=_api(["historical"], {}, calls),
    ):
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    assert len(calls) == 1 and calls[0].startswith("messages?")
    assert json.loads(watermark)["initialized"] is True


def test_real_list_then_full_message_emits_shipping_update(monkeypatch):
    scanner = PackageTrackingScanner()
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "token")
    config = scanner.configure()
    message = _message("msg123")
    with patch.object(
        scanner,
        "_gmail_api",
        side_effect=_api(["msg123"], {"msg123": message}),
    ):
        pollen, watermark = scanner.poll(config, _state(config))
    assert len(pollen) == 1
    item = pollen[0]
    assert item["id"] == "package-msg123"
    assert item["type"] == "package_shipped"
    assert item["metadata"]["tracking_number"] == "1ZABCDEF1234567890"
    assert item["metadata"]["carrier"] == "UPS"
    assert item["author_name"] == "Amazon.com"
    assert json.loads(watermark)["seen_ids"] == ["msg123"]


def test_newest_first_backlog_drains_oldest_first_without_advancing_time(monkeypatch):
    scanner = PackageTrackingScanner()
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "token")
    config = {**scanner.configure(), "max_items": 2}
    ids = ["newest", "middle", "oldest"]
    messages = {value: _message(value) for value in ids}
    state = _state(config)
    with patch.object(scanner, "_gmail_api", side_effect=_api(ids, messages)):
        pollen, watermark = scanner.poll(config, state)
    assert [item["id"] for item in pollen] == ["package-oldest", "package-middle"]
    assert json.loads(watermark)["internal_date_ms"] == json.loads(state)["internal_date_ms"]


def test_seen_opaque_ids_are_not_reemitted_in_overlap(monkeypatch):
    scanner = PackageTrackingScanner()
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "token")
    config = scanner.configure()
    with patch.object(
        scanner,
        "_gmail_api",
        side_effect=_api(["new", "seen"], {"new": _message("new")}),
    ):
        pollen, _ = scanner.poll(config, _state(config, seen=["seen"]))
    assert [item["id"] for item in pollen] == ["package-new"]


def test_detail_failure_commits_successful_ids_but_holds_time_boundary(monkeypatch):
    scanner = PackageTrackingScanner()
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "token")
    config = scanner.configure()
    original = _state(config)

    def fake(path, token):
        if path.startswith("messages?"):
            return {"messages": [{"id": "bad"}, {"id": "good"}]}
        if path.startswith("messages/good"):
            return _message("good")
        return None

    with patch.object(scanner, "_gmail_api", side_effect=fake):
        pollen, watermark = scanner.poll(config, original)
    assert [item["id"] for item in pollen] == ["package-good"]
    updated = json.loads(watermark)
    assert updated["seen_ids"] == ["good"]
    assert updated["internal_date_ms"] == json.loads(original)["internal_date_ms"]


def test_malformed_detail_does_not_advance_or_poison_baseline(monkeypatch):
    scanner = PackageTrackingScanner()
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "token")
    config = scanner.configure()
    state = _state(config)
    malformed = _message("bad")
    malformed["payload"]["headers"][0]["value"] = {"bad": "shape"}
    with patch.object(
        scanner,
        "_gmail_api",
        side_effect=_api(["bad"], {"bad": malformed}),
    ):
        assert scanner.poll(config, state) == ([], state)


def test_repeated_page_token_and_corrupt_state_fail_closed(monkeypatch):
    scanner = PackageTrackingScanner()
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "token")
    config = scanner.configure()
    state = _state(config)
    with patch.object(
        scanner,
        "_gmail_api",
        return_value={"messages": [], "nextPageToken": "repeat"},
    ):
        assert scanner.poll(config, state) == ([], state)

    corrupt = json.loads(state)
    corrupt["seen_ids"] = [123]
    corrupt_state = json.dumps(corrupt)
    with patch.object(scanner, "_gmail_api") as api:
        assert scanner.poll(config, corrupt_state) == ([], corrupt_state)
    api.assert_not_called()


def test_config_types_are_not_silently_coerced(monkeypatch):
    scanner = PackageTrackingScanner()
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "token")
    for config in (
        {**scanner.configure(), "search_query": {"query": "delivery"}},
        {**scanner.configure(), "search_query": " delivery"},
        {**scanner.configure(), "token_env": 123},
    ):
        with patch.object(scanner, "_gmail_api") as api:
            assert scanner.poll(config, "safe") == ([], "safe")
        api.assert_not_called()
