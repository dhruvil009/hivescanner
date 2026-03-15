"""Tests for triage_responder.py"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
import triage_responder


@pytest.fixture(autouse=True)
def tmp_hivescanner(tmp_path, monkeypatch):
    monkeypatch.setattr(triage_responder, "HIVESCANNER_HOME", tmp_path)
    monkeypatch.setattr(triage_responder, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(triage_responder, "POLLEN_FILE", tmp_path / "pollen.json")
    monkeypatch.setattr(triage_responder, "AUDIT_FILE", tmp_path / "audit.json")
    return tmp_path


class TestGenerateDraft:
    def test_blocked_when_triage_disabled(self):
        result = triage_responder.generate_draft(
            {"type": "mention", "title": "crash in prod"},
            {"triage": {"enabled": False}},
        )
        assert result["blocked"] is True

    def test_generates_crash_template(self):
        result = triage_responder.generate_draft(
            {"type": "mention", "title": "crash in prod", "preview": "app crashed", "url": "https://example.com"},
            {"triage": {"enabled": True}, "id": "test-group"},
        )
        assert result["blocked"] is False
        assert "crash ID" in result["draft"]

    def test_generates_sev_template(self):
        result = triage_responder.generate_draft(
            {"type": "mention", "title": "SEV2 incident", "preview": "sev2", "url": "https://example.com"},
            {"triage": {"enabled": True}, "id": "test-group"},
        )
        assert "impact scope" in result["draft"]

    def test_generates_default_template(self):
        result = triage_responder.generate_draft(
            {"type": "mention", "title": "question", "preview": "hello", "url": "https://example.com"},
            {"triage": {"enabled": True}, "id": "test-group"},
        )
        assert result["blocked"] is False
        assert "Related context" in result["draft"]

    def test_blocked_by_type_filter(self):
        result = triage_responder.generate_draft(
            {"type": "ci_passed", "title": "CI passed"},
            {"triage": {"enabled": True, "allowed_item_types": ["mention"]}},
        )
        assert result["blocked"] is True

    def test_blocked_by_keyword_filter(self):
        result = triage_responder.generate_draft(
            {"type": "mention", "title": "hello", "preview": "world"},
            {"triage": {"enabled": True, "trigger_keywords": ["crash", "sev"]}},
        )
        assert result["blocked"] is True


class TestContentSafety:
    def test_safe_content(self):
        assert triage_responder._content_safe("Hello, looking into this.") is True

    def test_unsafe_remediation(self):
        assert triage_responder._content_safe("you should try running this command") is False

    def test_unsafe_code_block(self):
        assert triage_responder._content_safe("```\nsome code\n```") is False


class TestAutonomy:
    def test_toggle(self, tmp_hivescanner):
        config = {"version": 1, "autonomy": {"enabled": False}}
        (tmp_hivescanner / "config.json").write_text(json.dumps(config))

        result = triage_responder.set_autonomy(True)
        assert result["autonomy_enabled"] is True

        status = triage_responder.autonomy_status()
        assert status["enabled"] is True

        result = triage_responder.set_autonomy(False)
        assert result["autonomy_enabled"] is False


class TestAuditLog:
    def test_audit_entries(self, tmp_hivescanner):
        triage_responder._log_audit("test_action", pollen_id="123")
        audit = triage_responder._load_audit()
        assert len(audit["entries"]) == 1
        assert audit["entries"][0]["action"] == "test_action"
        assert audit["entries"][0]["pollen_id"] == "123"
