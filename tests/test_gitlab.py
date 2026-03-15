"""Tests for GitLab scanner."""

import importlib.util
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_gitlab_adapter_path = os.path.join(
    os.path.dirname(__file__), "..", "community", "gitlab", "adapter.py"
)
_spec = importlib.util.spec_from_file_location("gitlab_adapter", _gitlab_adapter_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
GitLabScanner = _mod.GitLabScanner


SAMPLE_MR = {
    "iid": 42,
    "title": "Add feature X",
    "state": "opened",
    "project_id": 100,
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/42",
    "author": {"username": "bob", "name": "Bob"},
}

SAMPLE_PROJECT = {"id": 100, "name": "my-project"}

SAMPLE_PIPELINE = {
    "id": 999,
    "ref": "main",
    "status": "failed",
    "web_url": "https://gitlab.com/org/repo/-/pipelines/999",
}

SAMPLE_TODO = {
    "id": 555,
    "body": "You were mentioned in a comment",
    "action_name": "mentioned",
    "target_type": "MergeRequest",
    "target_url": "https://gitlab.com/org/repo/-/merge_requests/42",
    "target": {"title": "Add feature X"},
    "author": {"username": "alice", "name": "Alice"},
}

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}


@pytest.fixture
def scanner():
    return GitLabScanner()


def _make_config(**overrides):
    cfg = {
        "token_env": "GITLAB_TOKEN",
        "gitlab_url": "https://gitlab.com",
        "username": "dhruvil",
        "max_items": 20,
    }
    cfg.update(overrides)
    return cfg


class TestGitLabScanner:
    def test_configure_returns_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["token_env"] == "GITLAB_TOKEN"
        assert config["gitlab_url"] == "https://gitlab.com"
        assert config["username"] == ""
        assert config["max_items"] == 20

    def test_poll_empty_when_no_token(self, scanner):
        with patch.dict(os.environ, {}, clear=True):
            pollen, wm = scanner.poll(
                _make_config(),
                "2026-03-15T09:00:00Z",
            )
        assert pollen == []
        assert wm == "2026-03-15T09:00:00Z"

    def test_mr_review_emits_gitlab_mr_review(self, scanner):
        """Mock _api to return MR list, projects=[], todos=[]."""
        def fake_api(path, token, gitlab_url):
            if path.startswith("/merge_requests"):
                return [SAMPLE_MR]
            if path.startswith("/projects?membership"):
                return []
            if path.startswith("/todos"):
                return []
            return []

        with patch.dict(os.environ, {"GITLAB_TOKEN": "tok123"}), \
             patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                _make_config(),
                "2026-03-15T09:00:00Z",
            )

        mr_pollen = [p for p in pollen if p["type"] == "gitlab_mr_review"]
        assert len(mr_pollen) == 1
        p = mr_pollen[0]
        assert p["id"] == "gitlab-mr-42"
        assert p["source"] == "gitlab"
        assert p["title"] == "Add feature X"
        assert p["author"] == "bob"
        assert p["author_name"] == "Bob"
        assert p["group"] == "Merge Requests"
        assert p["url"] == "https://gitlab.com/org/repo/-/merge_requests/42"
        assert p["metadata"]["iid"] == 42
        assert p["metadata"]["state"] == "opened"
        assert p["metadata"]["project_id"] == 100

    def test_ci_failure_emits_gitlab_ci_failure(self, scanner):
        """Mock _api for projects then failed pipelines."""
        def fake_api(path, token, gitlab_url):
            if path.startswith("/merge_requests"):
                return []
            if path.startswith("/projects?membership"):
                return [SAMPLE_PROJECT]
            if path.startswith("/projects/100/pipelines"):
                return [SAMPLE_PIPELINE]
            if path.startswith("/todos"):
                return []
            return []

        with patch.dict(os.environ, {"GITLAB_TOKEN": "tok123"}), \
             patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                _make_config(),
                "2026-03-15T09:00:00Z",
            )

        ci_pollen = [p for p in pollen if p["type"] == "gitlab_ci_failure"]
        assert len(ci_pollen) == 1
        p = ci_pollen[0]
        assert p["id"] == "gitlab-ci-999"
        assert p["source"] == "gitlab"
        assert "999" in p["title"]
        assert "my-project" in p["title"]
        assert p["group"] == "CI Pipelines"
        assert p["url"] == "https://gitlab.com/org/repo/-/pipelines/999"
        assert p["metadata"]["pipeline_id"] == 999
        assert p["metadata"]["project_id"] == 100
        assert p["metadata"]["project_name"] == "my-project"
        assert p["metadata"]["ref"] == "main"
        assert p["metadata"]["status"] == "failed"

    def test_todo_emits_gitlab_mention(self, scanner):
        """Mock _api for todos."""
        def fake_api(path, token, gitlab_url):
            if path.startswith("/merge_requests"):
                return []
            if path.startswith("/projects?membership"):
                return []
            if path.startswith("/todos"):
                return [SAMPLE_TODO]
            return []

        with patch.dict(os.environ, {"GITLAB_TOKEN": "tok123"}), \
             patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                _make_config(),
                "2026-03-15T09:00:00Z",
            )

        todo_pollen = [p for p in pollen if p["type"] == "gitlab_mention"]
        assert len(todo_pollen) == 1
        p = todo_pollen[0]
        assert p["id"] == "gitlab-todo-555"
        assert p["source"] == "gitlab"
        assert p["title"] == "Add feature X"
        assert p["preview"] == "You were mentioned in a comment"
        assert p["author"] == "alice"
        assert p["author_name"] == "Alice"
        assert p["group"] == "Mentions"
        assert p["url"] == "https://gitlab.com/org/repo/-/merge_requests/42"
        assert p["metadata"]["todo_id"] == 555
        assert p["metadata"]["action_name"] == "mentioned"
        assert p["metadata"]["target_type"] == "MergeRequest"

    def test_pollen_schema_has_all_required_keys(self, scanner):
        """All three pollen types should have every required key."""
        def fake_api(path, token, gitlab_url):
            if path.startswith("/merge_requests"):
                return [SAMPLE_MR]
            if path.startswith("/projects?membership"):
                return [SAMPLE_PROJECT]
            if path.startswith("/projects/100/pipelines"):
                return [SAMPLE_PIPELINE]
            if path.startswith("/todos"):
                return [SAMPLE_TODO]
            return []

        with patch.dict(os.environ, {"GITLAB_TOKEN": "tok123"}), \
             patch.object(scanner, "_api", side_effect=fake_api):
            pollen, wm = scanner.poll(
                _make_config(),
                "2026-03-15T09:00:00Z",
            )

        assert len(pollen) == 3
        for p in pollen:
            missing = REQUIRED_POLLEN_KEYS - set(p.keys())
            assert not missing, f"Pollen {p['id']} missing keys: {missing}"

    def test_api_error_preserves_watermark(self, scanner):
        """When _api returns None (error), watermark should not advance."""
        original_wm = "2026-03-15T09:00:00Z"
        with patch.dict(os.environ, {"GITLAB_TOKEN": "tok123"}), \
             patch.object(scanner, "_api") as mock_api:
            mock_api.return_value = None
            pollen, wm = scanner.poll(
                _make_config(),
                original_wm,
            )

        assert wm == original_wm
