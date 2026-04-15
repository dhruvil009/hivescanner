"""Tests for Sentry scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch

import pytest

_sentry_adapter_path = os.path.join(os.path.dirname(__file__), "..", "community", "sentry", "adapter.py")
_spec = importlib.util.spec_from_file_location("sentry_adapter", _sentry_adapter_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SentryScanner = _mod.SentryScanner


SAMPLE_ISSUE = {
    "id": "12345",
    "title": "ValueError: invalid literal",
    "level": "error",
    "platform": "python",
    "count": "50",
    "lastSeen": "2026-03-15T12:00:00Z",
    "permalink": "https://sentry.io/issues/12345/",
    "shortId": "PROJ-1A",
    "isSubscribed": False,
}

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}


@pytest.fixture
def scanner():
    return SentryScanner()


class TestSentryScanner:

    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["token_env"] == "SENTRY_TOKEN"
        assert config["organization"] == ""
        assert config["project"] == ""
        assert config["max_items"] == 20

    def test_poll_empty_when_no_token(self, scanner, monkeypatch):
        monkeypatch.delenv("SENTRY_TOKEN", raising=False)
        pollen, wm = scanner.poll(
            {"token_env": "SENTRY_TOKEN", "organization": "my-org"},
            "2026-03-15T00:00:00Z",
        )
        assert pollen == []
        assert wm == "2026-03-15T00:00:00Z"

    def test_poll_empty_when_no_organization(self, scanner, monkeypatch):
        monkeypatch.setenv("SENTRY_TOKEN", "test-token")
        pollen, wm = scanner.poll(
            {"token_env": "SENTRY_TOKEN", "organization": ""},
            "2026-03-15T00:00:00Z",
        )
        assert pollen == []
        assert wm == "2026-03-15T00:00:00Z"

    def test_regular_issue_emits_sentry_issue(self, scanner, monkeypatch):
        monkeypatch.setenv("SENTRY_TOKEN", "test-token")
        issue = {**SAMPLE_ISSUE, "isSubscribed": False, "count": "50"}

        with patch.object(scanner, "_api", return_value=[issue]):
            pollen, wm = scanner.poll(
                {"token_env": "SENTRY_TOKEN", "organization": "my-org", "project": "", "max_items": 20},
                "",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "sentry_issue"
        # lastSeen is hashed into the id so new event waves re-surface as fresh pollen.
        assert pollen[0]["id"].startswith("sentry-12345-")
        assert len(pollen[0]["id"]) == len("sentry-12345-") + 8
        assert pollen[0]["source"] == "sentry"

    def test_new_event_waves_produce_distinct_ids(self, scanner, monkeypatch):
        """Same issue seen at different lastSeen times must yield distinct pollen IDs."""
        monkeypatch.setenv("SENTRY_TOKEN", "test-token")
        ids = []
        for last_seen in ("2026-03-15T12:00:00Z", "2026-03-15T14:00:00Z", "2026-03-15T16:00:00Z"):
            issue = {**SAMPLE_ISSUE, "lastSeen": last_seen}
            with patch.object(scanner, "_api", return_value=[issue]):
                pollen, _ = scanner.poll(
                    {"token_env": "SENTRY_TOKEN", "organization": "my-org", "project": "", "max_items": 20},
                    "",
                )
            ids.append(pollen[0]["id"])

        assert len(set(ids)) == 3, "each new event wave must produce a distinct id"

    def test_subscribed_issue_emits_sentry_spike(self, scanner, monkeypatch):
        monkeypatch.setenv("SENTRY_TOKEN", "test-token")
        issue = {**SAMPLE_ISSUE, "isSubscribed": True, "count": "50"}

        with patch.object(scanner, "_api", return_value=[issue]):
            pollen, wm = scanner.poll(
                {"token_env": "SENTRY_TOKEN", "organization": "my-org", "project": "", "max_items": 20},
                "",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "sentry_spike"

    def test_high_count_issue_emits_sentry_spike(self, scanner, monkeypatch):
        monkeypatch.setenv("SENTRY_TOKEN", "test-token")
        issue = {**SAMPLE_ISSUE, "isSubscribed": False, "count": "200"}

        with patch.object(scanner, "_api", return_value=[issue]):
            pollen, wm = scanner.poll(
                {"token_env": "SENTRY_TOKEN", "organization": "my-org", "project": "", "max_items": 20},
                "",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "sentry_spike"

    def test_watermark_filters_old_issues(self, scanner, monkeypatch):
        monkeypatch.setenv("SENTRY_TOKEN", "test-token")
        old_issue = {**SAMPLE_ISSUE, "lastSeen": "2026-03-14T10:00:00Z"}

        with patch.object(scanner, "_api", return_value=[old_issue]):
            pollen, wm = scanner.poll(
                {"token_env": "SENTRY_TOKEN", "organization": "my-org", "project": "", "max_items": 20},
                "2026-03-15T00:00:00Z",
            )

        assert pollen == []

    def test_watermark_advances_to_newest_lastseen(self, scanner, monkeypatch):
        monkeypatch.setenv("SENTRY_TOKEN", "test-token")
        issue_a = {**SAMPLE_ISSUE, "id": "111", "lastSeen": "2026-03-15T14:00:00Z"}
        issue_b = {**SAMPLE_ISSUE, "id": "222", "lastSeen": "2026-03-15T16:00:00Z"}
        issue_c = {**SAMPLE_ISSUE, "id": "333", "lastSeen": "2026-03-15T15:00:00Z"}

        with patch.object(scanner, "_api", return_value=[issue_a, issue_b, issue_c]):
            pollen, wm = scanner.poll(
                {"token_env": "SENTRY_TOKEN", "organization": "my-org", "project": "", "max_items": 20},
                "2026-03-15T12:00:00Z",
            )

        assert len(pollen) == 3
        assert wm == "2026-03-15T16:00:00Z"

    def test_pollen_schema_has_all_required_keys(self, scanner, monkeypatch):
        monkeypatch.setenv("SENTRY_TOKEN", "test-token")

        with patch.object(scanner, "_api", return_value=[SAMPLE_ISSUE]):
            pollen, wm = scanner.poll(
                {"token_env": "SENTRY_TOKEN", "organization": "my-org", "project": "", "max_items": 20},
                "",
            )

        assert len(pollen) == 1
        missing = REQUIRED_POLLEN_KEYS - set(pollen[0].keys())
        assert not missing, f"Pollen missing keys: {missing}"
        # Verify metadata fields
        meta = pollen[0]["metadata"]
        assert "level" in meta
        assert "platform" in meta
        assert "count" in meta
        assert "last_seen" in meta

    def test_api_error_returns_empty(self, scanner, monkeypatch):
        monkeypatch.setenv("SENTRY_TOKEN", "test-token")

        with patch.object(scanner, "_api", return_value=None):
            pollen, wm = scanner.poll(
                {"token_env": "SENTRY_TOKEN", "organization": "my-org", "project": "", "max_items": 20},
                "2026-03-15T00:00:00Z",
            )

        assert pollen == []
        assert wm == "2026-03-15T00:00:00Z"
