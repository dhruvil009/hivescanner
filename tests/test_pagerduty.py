"""Tests for PagerDuty scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch

import pytest

_pd_adapter_path = os.path.join(os.path.dirname(__file__), "..", "community", "pagerduty", "adapter.py")
_spec = importlib.util.spec_from_file_location("pagerduty_adapter", _pd_adapter_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
PagerDutyScanner = _mod.PagerDutyScanner


SAMPLE_INCIDENT = {
    "id": "P123ABC",
    "status": "triggered",
    "title": "High CPU on web-1",
    "urgency": "high",
    "service": {"summary": "web-service"},
    "assignments": [{"assignee": {"id": "PUSER1", "summary": "Alice"}}],
    "html_url": "https://myco.pagerduty.com/incidents/P123ABC",
    "incident_number": 42,
}

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}


@pytest.fixture
def scanner():
    return PagerDutyScanner()


class TestPagerDutyScanner:

    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["token_env"] == "PAGERDUTY_TOKEN"
        assert config["user_id"] == ""
        assert config["max_items"] == 20

    def test_poll_empty_when_no_token(self, scanner, monkeypatch):
        monkeypatch.delenv("PAGERDUTY_TOKEN", raising=False)
        pollen, wm = scanner.poll(
            {"token_env": "PAGERDUTY_TOKEN"},
            "2026-03-15T00:00:00Z",
        )
        assert pollen == []
        assert wm == "2026-03-15T00:00:00Z"

    def test_triggered_incident_emits_pagerduty_triggered(self, scanner, monkeypatch):
        monkeypatch.setenv("PAGERDUTY_TOKEN", "test-token")
        incident = {**SAMPLE_INCIDENT, "status": "triggered"}

        with patch.object(scanner, "_api", return_value={"incidents": [incident]}):
            pollen, wm = scanner.poll(
                {"token_env": "PAGERDUTY_TOKEN", "max_items": 20},
                "2026-03-15T00:00:00Z",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "pagerduty_triggered"
        # Status is encoded in the id so triggered→acknowledged→resolved produce distinct pollen.
        assert pollen[0]["id"] == "pagerduty-P123ABC-triggered"
        assert pollen[0]["source"] == "pagerduty"

    def test_incident_transitions_produce_distinct_ids(self, scanner, monkeypatch):
        """triggered → acknowledged → resolved must yield three distinct pollen IDs."""
        monkeypatch.setenv("PAGERDUTY_TOKEN", "test-token")
        ids = []
        for status in ("triggered", "acknowledged", "resolved"):
            incident = {**SAMPLE_INCIDENT, "status": status}
            with patch.object(scanner, "_api", return_value={"incidents": [incident]}):
                pollen, _ = scanner.poll(
                    {"token_env": "PAGERDUTY_TOKEN", "max_items": 20},
                    "2026-03-15T00:00:00Z",
                )
            ids.append(pollen[0]["id"])

        assert ids == [
            "pagerduty-P123ABC-triggered",
            "pagerduty-P123ABC-acknowledged",
            "pagerduty-P123ABC-resolved",
        ]

    def test_acknowledged_incident_emits_pagerduty_incident(self, scanner, monkeypatch):
        monkeypatch.setenv("PAGERDUTY_TOKEN", "test-token")
        incident = {**SAMPLE_INCIDENT, "status": "acknowledged"}

        with patch.object(scanner, "_api", return_value={"incidents": [incident]}):
            pollen, wm = scanner.poll(
                {"token_env": "PAGERDUTY_TOKEN", "max_items": 20},
                "2026-03-15T00:00:00Z",
            )

        assert len(pollen) == 1
        assert pollen[0]["type"] == "pagerduty_incident"

    def test_user_id_filter_included_in_url(self, scanner, monkeypatch):
        monkeypatch.setenv("PAGERDUTY_TOKEN", "test-token")

        with patch.object(scanner, "_api", return_value={"incidents": []}) as mock_api:
            scanner.poll(
                {"token_env": "PAGERDUTY_TOKEN", "user_id": "PUSER1", "max_items": 20},
                "2026-03-15T00:00:00Z",
            )

        call_url = mock_api.call_args[0][0]
        assert "user_ids[]=PUSER1" in call_url

    def test_pollen_schema_has_all_required_keys(self, scanner, monkeypatch):
        monkeypatch.setenv("PAGERDUTY_TOKEN", "test-token")

        with patch.object(scanner, "_api", return_value={"incidents": [SAMPLE_INCIDENT]}):
            pollen, wm = scanner.poll(
                {"token_env": "PAGERDUTY_TOKEN", "max_items": 20},
                "2026-03-15T00:00:00Z",
            )

        assert len(pollen) == 1
        missing = REQUIRED_POLLEN_KEYS - set(pollen[0].keys())
        assert not missing, f"Pollen missing keys: {missing}"
        # Verify metadata fields
        meta = pollen[0]["metadata"]
        assert "urgency" in meta
        assert "service_name" in meta
        assert "status" in meta
        assert "incident_number" in meta

    def test_api_error_returns_empty(self, scanner, monkeypatch):
        monkeypatch.setenv("PAGERDUTY_TOKEN", "test-token")

        with patch.object(scanner, "_api", return_value=None):
            pollen, wm = scanner.poll(
                {"token_env": "PAGERDUTY_TOKEN", "max_items": 20},
                "2026-03-15T00:00:00Z",
            )

        assert pollen == []
        assert wm == "2026-03-15T00:00:00Z"
