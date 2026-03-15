"""Tests for Jira scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_jira_adapter_path = os.path.join(
    os.path.dirname(__file__), "..", "community", "jira", "adapter.py"
)
_spec = importlib.util.spec_from_file_location("jira_adapter", _jira_adapter_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
JiraScanner = _mod.JiraScanner


SAMPLE_ISSUE = {
    "key": "PROJ-123",
    "fields": {
        "summary": "Fix login bug",
        "status": {"name": "In Progress"},
        "priority": {"name": "High"},
        "issuetype": {"name": "Bug"},
        "assignee": {"displayName": "Alice", "emailAddress": "alice@co.com"},
        "description": "Something is broken",
    },
}

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}


@pytest.fixture
def scanner():
    return JiraScanner()


class TestJiraScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["token_env"] == "JIRA_TOKEN"
        assert config["domain"] == ""
        assert config["username"] == ""
        assert config["max_items"] == 20

    def test_poll_empty_when_no_token(self, scanner):
        with patch.dict(os.environ, {}, clear=True):
            pollen, wm = scanner.poll(
                {"token_env": "JIRA_TOKEN", "domain": "myco.atlassian.net", "username": "alice@co.com"},
                "2026-03-15T09:00:00Z",
            )
        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"

    def test_poll_empty_when_no_domain(self, scanner):
        with patch.dict(os.environ, {"JIRA_TOKEN": "tok123"}):
            pollen, wm = scanner.poll(
                {"token_env": "JIRA_TOKEN", "domain": "", "username": "alice@co.com"},
                "2026-03-15T09:00:00Z",
            )
        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"

    def test_assigned_issue_emits_jira_assigned(self, scanner):
        """Assignee email matches username -> jira_assigned."""
        issue = {
            "key": "PROJ-123",
            "fields": {
                "summary": "Fix login bug",
                "status": {"name": "In Progress"},
                "priority": {"name": "High"},
                "issuetype": {"name": "Bug"},
                "assignee": {"displayName": "Alice", "emailAddress": "alice@co.com"},
                "description": "Something is broken",
            },
        }
        with patch.dict(os.environ, {"JIRA_TOKEN": "tok123"}), \
             patch.object(scanner, "_api") as mock_api:
            mock_api.return_value = {"issues": [issue]}
            pollen, wm = scanner.poll(
                {"token_env": "JIRA_TOKEN", "domain": "myco.atlassian.net",
                 "username": "alice@co.com", "max_items": 20},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "jira_assigned"
        assert p["id"] == "jira-PROJ-123"
        assert p["source"] == "jira"
        assert p["author"] == "alice@co.com"
        assert p["author_name"] == "Alice"
        assert p["group"] == "Issues"
        assert "PROJ-123" in p["url"]
        assert p["metadata"]["issue_key"] == "PROJ-123"
        assert p["metadata"]["status"] == "In Progress"
        assert p["metadata"]["priority"] == "High"
        assert p["metadata"]["issue_type"] == "Bug"

    def test_mentioned_issue_emits_jira_mentioned(self, scanner):
        """Username appears in description -> jira_mentioned."""
        issue = {
            "key": "PROJ-456",
            "fields": {
                "summary": "Review deployment",
                "status": {"name": "Open"},
                "priority": {"name": "Medium"},
                "issuetype": {"name": "Task"},
                "assignee": {"displayName": "Bob", "emailAddress": "bob@co.com"},
                "description": "Please check with alice@co.com about the rollout plan",
            },
        }
        with patch.dict(os.environ, {"JIRA_TOKEN": "tok123"}), \
             patch.object(scanner, "_api") as mock_api:
            mock_api.return_value = {"issues": [issue]}
            pollen, wm = scanner.poll(
                {"token_env": "JIRA_TOKEN", "domain": "myco.atlassian.net",
                 "username": "alice@co.com", "max_items": 20},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "jira_mentioned"
        assert p["id"] == "jira-PROJ-456"

    def test_updated_issue_emits_jira_updated(self, scanner):
        """No assignee match, no mention -> jira_updated."""
        issue = {
            "key": "PROJ-789",
            "fields": {
                "summary": "Update docs",
                "status": {"name": "Done"},
                "priority": {"name": "Low"},
                "issuetype": {"name": "Story"},
                "assignee": {"displayName": "Charlie", "emailAddress": "charlie@co.com"},
                "description": "Documentation needs updating",
            },
        }
        with patch.dict(os.environ, {"JIRA_TOKEN": "tok123"}), \
             patch.object(scanner, "_api") as mock_api:
            mock_api.return_value = {"issues": [issue]}
            pollen, wm = scanner.poll(
                {"token_env": "JIRA_TOKEN", "domain": "myco.atlassian.net",
                 "username": "alice@co.com", "max_items": 20},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        p = pollen[0]
        assert p["type"] == "jira_updated"
        assert p["id"] == "jira-PROJ-789"

    def test_pollen_schema_has_all_required_keys(self, scanner):
        with patch.dict(os.environ, {"JIRA_TOKEN": "tok123"}), \
             patch.object(scanner, "_api") as mock_api:
            mock_api.return_value = {"issues": [SAMPLE_ISSUE]}
            pollen, wm = scanner.poll(
                {"token_env": "JIRA_TOKEN", "domain": "myco.atlassian.net",
                 "username": "alice@co.com", "max_items": 20},
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 1
        for p in pollen:
            missing = REQUIRED_POLLEN_KEYS - set(p.keys())
            assert not missing, f"Pollen missing keys: {missing}"

    def test_api_error_returns_empty(self, scanner):
        original_wm = "2026-03-15T09:00:00Z"
        with patch.dict(os.environ, {"JIRA_TOKEN": "tok123"}), \
             patch.object(scanner, "_api") as mock_api:
            mock_api.return_value = None
            pollen, wm = scanner.poll(
                {"token_env": "JIRA_TOKEN", "domain": "myco.atlassian.net",
                 "username": "alice@co.com", "max_items": 20},
                original_wm,
            )

        assert pollen == []
        assert wm == original_wm
