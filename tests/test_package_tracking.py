"""Tests for Package Tracking scanner."""

import base64
import importlib.util
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

# Load module from hyphenated directory name
spec = importlib.util.spec_from_file_location(
    "package_tracking_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "package-tracking", "adapter.py"),
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
PackageTrackingScanner = mod.PackageTrackingScanner

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}


@pytest.fixture
def scanner():
    return PackageTrackingScanner()


class TestPackageTrackingConfigure:

    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["token_env"] == "GOOGLE_ACCESS_TOKEN"
        assert config["max_items"] == 10
        assert "shipped" in config["search_query"]


class TestPollNoToken:

    def test_poll_empty_when_no_token(self, scanner):
        with patch.dict(os.environ, {}, clear=True):
            pollen, wm = scanner.poll({"token_env": "GOOGLE_ACCESS_TOKEN"}, "old-wm")
        assert pollen == []
        assert wm == "old-wm"


class TestExtractTrackingNumber:

    def test_extract_tracking_ups(self, scanner):
        text = "Your package 1ZABCDEF1234567890 has shipped"
        tracking, carrier = scanner._extract_tracking_number(text)
        assert tracking == "1ZABCDEF1234567890"
        assert carrier == "UPS"

    def test_extract_tracking_usps(self, scanner):
        text = "Tracking: 9400111899223100012345"
        tracking, carrier = scanner._extract_tracking_number(text)
        assert tracking == "9400111899223100012345"
        assert carrier == "USPS"

    def test_extract_tracking_fedex(self, scanner):
        text = "Your FedEx tracking number is 123456789012"
        tracking, carrier = scanner._extract_tracking_number(text)
        assert tracking == "123456789012"
        assert carrier == "FedEx"

    def test_extract_tracking_none(self, scanner):
        text = "Thank you for your order! No tracking yet."
        tracking, carrier = scanner._extract_tracking_number(text)
        assert tracking == ""
        assert carrier == ""


class TestDetectEventType:

    def test_detect_event_shipped(self, scanner):
        assert scanner._detect_event_type("Your order has shipped!") == "package_shipped"

    def test_detect_event_out_for_delivery(self, scanner):
        assert scanner._detect_event_type("Package is out for delivery") == "package_out_for_delivery"

    def test_detect_event_delivered(self, scanner):
        assert scanner._detect_event_type("Your package has been delivered") == "package_delivered"

    def test_detect_event_default_is_shipped(self, scanner):
        assert scanner._detect_event_type("some random text") == "package_shipped"


class TestGetHeader:

    def test_get_header(self, scanner):
        headers = [
            {"name": "Subject", "value": "Your order shipped"},
            {"name": "From", "value": "Amazon <ship@amazon.com>"},
        ]
        assert scanner._get_header(headers, "Subject") == "Your order shipped"
        assert scanner._get_header(headers, "from") == "Amazon <ship@amazon.com>"
        assert scanner._get_header(headers, "X-Missing") == ""


class TestDecodeBody:

    def test_decode_body_simple(self, scanner):
        raw_text = "Your package 1ZABCDEF1234567890 has shipped"
        encoded = base64.urlsafe_b64encode(raw_text.encode()).decode()
        payload = {"body": {"data": encoded}}
        assert scanner._decode_body(payload) == raw_text

    def test_decode_body_multipart(self, scanner):
        raw_text = "Multipart plain text body"
        encoded = base64.urlsafe_b64encode(raw_text.encode()).decode()
        payload = {
            "body": {},
            "parts": [
                {"mimeType": "text/html", "body": {"data": "ignored"}},
                {"mimeType": "text/plain", "body": {"data": encoded}},
            ],
        }
        assert scanner._decode_body(payload) == raw_text

    def test_decode_body_nested_multipart(self, scanner):
        raw_text = "Nested body text"
        encoded = base64.urlsafe_b64encode(raw_text.encode()).decode()
        payload = {
            "body": {},
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "body": {},
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": encoded}},
                    ],
                }
            ],
        }
        assert scanner._decode_body(payload) == raw_text

    def test_decode_body_empty(self, scanner):
        payload = {"body": {}}
        assert scanner._decode_body(payload) == ""


class TestPollWithShippingEmail:

    def _build_gmail_message(self, msg_id, subject, sender, body_text, internal_date_ms):
        encoded_body = base64.urlsafe_b64encode(body_text.encode()).decode()
        return {
            "id": msg_id,
            "internalDate": str(internal_date_ms),
            "payload": {
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": sender},
                ],
                "body": {"data": encoded_body},
            },
        }

    def test_poll_with_shipping_email(self, scanner):
        msg = self._build_gmail_message(
            "msg123",
            "Your order has shipped!",
            "Amazon.com <ship@amazon.com>",
            "Your package 1ZABCDEF1234567890 has shipped.",
            1710500000000,  # some epoch ms
        )
        search_result = {"messages": [{"id": "msg123"}]}

        def fake_gmail_api(path, token):
            if "messages?" in path:
                return search_result
            if "messages/msg123" in path:
                return msg
            return None

        with patch.dict(os.environ, {"GOOGLE_ACCESS_TOKEN": "fake-token"}), \
             patch.object(scanner, "_gmail_api", side_effect=fake_gmail_api):
            config = scanner.configure()
            config["token_env"] = "GOOGLE_ACCESS_TOKEN"
            pollen, wm = scanner.poll(config, "")

        assert len(pollen) == 1
        assert pollen[0]["type"] == "package_shipped"
        assert pollen[0]["id"] == "package-msg123"
        assert pollen[0]["source"] == "package-tracking"
        assert pollen[0]["metadata"]["tracking_number"] == "1ZABCDEF1234567890"
        assert pollen[0]["metadata"]["carrier"] == "UPS"
        assert pollen[0]["author_name"] == "Amazon.com"

    def test_watermark_filters_old_messages(self, scanner):
        # Message is from 2026-03-14T00:00:00Z  (epoch ms = 1773532800000)
        old_epoch_ms = int(datetime(2026, 3, 14, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        msg = self._build_gmail_message(
            "old_msg",
            "Your order shipped",
            "Store <noreply@store.com>",
            "Shipped with tracking 1ZABCDEF1234567890",
            old_epoch_ms,
        )
        search_result = {"messages": [{"id": "old_msg"}]}

        def fake_gmail_api(path, token):
            if "messages?" in path:
                return search_result
            if "messages/old_msg" in path:
                return msg
            return None

        # Watermark is after the message
        watermark = "2026-03-14T12:00:00Z"

        with patch.dict(os.environ, {"GOOGLE_ACCESS_TOKEN": "fake-token"}), \
             patch.object(scanner, "_gmail_api", side_effect=fake_gmail_api):
            config = scanner.configure()
            config["token_env"] = "GOOGLE_ACCESS_TOKEN"
            pollen, wm = scanner.poll(config, watermark)

        assert len(pollen) == 0

    def test_pollen_schema_has_all_required_keys(self, scanner):
        msg = self._build_gmail_message(
            "msg456",
            "Package delivered",
            "Retailer <noreply@retailer.com>",
            "Your package has been delivered.",
            1710600000000,
        )
        search_result = {"messages": [{"id": "msg456"}]}

        def fake_gmail_api(path, token):
            if "messages?" in path:
                return search_result
            if "messages/msg456" in path:
                return msg
            return None

        with patch.dict(os.environ, {"GOOGLE_ACCESS_TOKEN": "fake-token"}), \
             patch.object(scanner, "_gmail_api", side_effect=fake_gmail_api):
            config = scanner.configure()
            config["token_env"] = "GOOGLE_ACCESS_TOKEN"
            pollen, wm = scanner.poll(config, "")

        assert len(pollen) > 0, "Expected at least one pollen item"
        for item in pollen:
            missing = REQUIRED_POLLEN_KEYS - set(item.keys())
            assert not missing, f"Pollen missing keys: {missing}"
