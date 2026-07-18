"""Functional tests for GitLab's MR, pipeline, and todo APIs."""

import hashlib
import importlib.util
import json
import os
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "gitlab_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "gitlab", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
GitLabScanner = _mod.GitLabScanner


MR = {
    "iid": 42,
    "title": "Add feature X",
    "state": "opened",
    "project_id": 100,
    "updated_at": "2026-07-15T10:00:00Z",
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/42",
    "author": {"username": "bob", "name": "Bob"},
}
PIPELINE = {
    "id": 999,
    "ref": "main",
    "status": "failed",
    "updated_at": "2026-07-15T10:01:00Z",
    "web_url": "https://gitlab.com/org/repo/-/pipelines/999",
}
TODO = {
    "id": 555,
    "body": "You were mentioned in a comment",
    "action_name": "mentioned",
    "target_type": "MergeRequest",
    "target_url": "https://gitlab.com/org/repo/-/merge_requests/42",
    "target": {"title": "Add feature X"},
    "author": {"username": "alice", "name": "Alice"},
}


def _config(scanner, **changes):
    return {
        **scanner.configure(),
        "username": "dhruvil",
        "watch_projects": ["org/repo"],
        **changes,
    }


def _scope(config):
    value = {
        "gitlab_url": config["gitlab_url"],
        "username": config["username"],
        "projects": sorted(config["watch_projects"]),
        "reviews": bool(config["watch_reviews"]),
        "pipelines": bool(config["watch_pipelines"]),
        "todos": bool(config["watch_todos"]),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _state(config, *, mrs=None, todo_max=0):
    return json.dumps({
        "version": 3,
        "updated_at": "2026-07-15T09:00:00Z",
        "initialized": True,
        "scope": _scope(config),
        "merge_requests": mrs or {},
        "todo_max_id": todo_max,
    })


def _api(*, mrs=None, pipelines=None, todos=None):
    def fake(path, token, gitlab_url):
        if path.startswith("/merge_requests?"):
            return list(mrs or [])
        if path.startswith("/projects/") and "/pipelines?" in path:
            return list(pipelines or [])
        if path.startswith("/todos?"):
            return list(todos or [])
        return []

    return fake


def test_defaults_are_bounded_and_self_hostable():
    config = GitLabScanner().configure()
    assert config["token_env"] == "GITLAB_TOKEN"
    assert config["gitlab_url"] == "https://gitlab.com"
    assert config["max_items"] == 100
    assert config["max_pages"] == 3
    assert config["projects_per_poll"] == 3
    assert config["overlap_minutes"] == 10


def test_missing_token_preserves_watermark(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    assert scanner.poll(_config(scanner), "safe") == ([], "safe")


def test_first_scope_poll_bootstraps_quietly(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    config = _config(scanner)
    with patch.object(scanner, "_api", side_effect=_api(mrs=[MR], pipelines=[PIPELINE], todos=[TODO])):
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    state = json.loads(watermark)
    assert state["initialized"] is True
    assert state["scope"] == _scope(config)
    assert state["merge_requests"] == {"100:42": MR["updated_at"]}
    assert state["todo_max_id"] == 555


def test_new_review_assignment_uses_project_qualified_identity(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    config = _config(scanner, watch_pipelines=False, watch_todos=False)
    with patch.object(scanner, "_api", side_effect=_api(mrs=[MR])):
        pollen, watermark = scanner.poll(config, _state(config))
    assert len(pollen) == 1
    item = pollen[0]
    assert item["type"] == "gitlab_mr_review"
    assert item["id"].startswith("gitlab-mr-100-42-")
    assert item["metadata"]["project_id"] == "100"
    assert json.loads(watermark)["merge_requests"] == {"100:42": MR["updated_at"]}


def test_existing_review_request_is_not_repeated(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    config = _config(scanner, watch_pipelines=False, watch_todos=False)
    with patch.object(scanner, "_api", side_effect=_api(mrs=[MR])):
        pollen, _ = scanner.poll(config, _state(config, mrs={"100:42": "old"}))
    assert pollen == []


def test_failed_pipeline_query_uses_overlap_and_encoded_project(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    config = _config(scanner, watch_reviews=False, watch_todos=False)
    with patch.object(scanner, "_api", side_effect=_api(pipelines=[PIPELINE])) as api:
        pollen, _ = scanner.poll(config, _state(config))
    assert [item["type"] for item in pollen] == ["gitlab_ci_failure"]
    item = pollen[0]
    assert item["metadata"]["project_id"] == "org/repo"
    assert item["id"].startswith("gitlab-ci-") and item["id"].endswith("-999")
    path = api.call_args.args[0]
    assert "/projects/org%2Frepo/pipelines?" in path
    assert "updated_after=2026-07-15T08%3A50%3A00Z" in path


def test_only_todos_above_committed_numeric_id_emit(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    config = _config(scanner, watch_reviews=False, watch_pipelines=False)
    todos = [{**TODO, "id": 554}, TODO]
    with patch.object(scanner, "_api", side_effect=_api(todos=todos)):
        pollen, watermark = scanner.poll(config, _state(config, todo_max=554))
    assert [item["id"] for item in pollen] == ["gitlab-todo-555"]
    assert json.loads(watermark)["todo_max_id"] == 555


def test_scope_change_is_quiet_instead_of_replaying_other_instance(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    old = _config(scanner, watch_projects=["old/repo"])
    new = _config(scanner, watch_projects=["new/repo"])
    with patch.object(scanner, "_api", side_effect=_api(mrs=[MR], pipelines=[PIPELINE], todos=[TODO])):
        pollen, _ = scanner.poll(new, _state(old))
    assert pollen == []


def test_pagination_exhaustion_and_api_errors_fail_closed(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    config = _config(
        scanner,
        watch_pipelines=False,
        watch_todos=False,
        max_items=1,
        max_pages=1,
    )
    with patch.object(scanner, "_api", return_value=[MR]):
        assert scanner.poll(config, _state(config)) == ([], _state(config))
    with patch.object(scanner, "_api", return_value=None):
        assert scanner.poll(config, _state(config)) == ([], _state(config))


def test_invalid_base_url_and_configuration_are_rejected(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    for url in ["http://gitlab.example", "https://user:pass@gitlab.example", "https://x:bad"]:
        assert scanner.poll(_config(scanner, gitlab_url=url), "safe") == ([], "safe")
    assert scanner.poll(_config(scanner, watch_projects=["x"] * 101), "safe") == ([], "safe")


def test_project_budget_rotates_across_large_watch_list(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    projects = [f"org/repo-{index}" for index in range(4)]
    config = _config(
        scanner,
        watch_projects=projects,
        watch_reviews=False,
        watch_todos=False,
        projects_per_poll=2,
    )
    paths = []

    def fake(path, token, gitlab_url):
        paths.append(path)
        return []

    with patch.object(scanner, "_api", side_effect=fake):
        _, watermark = scanner.poll(config, _state(config))
        scanner.poll(config, watermark)

    assert "org%2Frepo-0" in paths[0] and "org%2Frepo-1" in paths[1]
    assert "org%2Frepo-2" in paths[2] and "org%2Frepo-3" in paths[3]


def test_review_failure_does_not_roll_back_pipeline_progress(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    config = _config(scanner, watch_todos=False)
    previous_mrs = {"200:7": "2026-07-15T08:00:00Z"}

    def fake(path, token, gitlab_url):
        if path.startswith("/merge_requests?"):
            return None
        if "/pipelines?" in path:
            return [PIPELINE]
        return []

    with patch.object(scanner, "_api", side_effect=fake):
        pollen, watermark = scanner.poll(
            config, _state(config, mrs=previous_mrs)
        )

    assert [item["type"] for item in pollen] == ["gitlab_ci_failure"]
    updated = json.loads(watermark)
    assert updated["merge_requests"] == previous_mrs
    assert "org/repo" in updated["project_updated_at"]


def test_malformed_pipeline_component_preserves_its_cursor(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    config = _config(scanner, watch_reviews=False, watch_todos=False)
    state = json.loads(_state(config))
    state.update({
        "version": 4,
        "project_updated_at": {"org/repo": "2026-07-15T09:00:00Z"},
        "project_next_index": 0,
        "reviews_initialized": False,
        "todos_initialized": False,
    })
    watermark = json.dumps(state, sort_keys=True, separators=(",", ":"))
    malformed = {**PIPELINE, "status": "running"}

    with patch.object(scanner, "_api", return_value=[malformed]):
        assert scanner.poll(config, watermark) == ([], watermark)


def test_restored_older_todo_emits_after_leaving_pending_set(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    config = _config(scanner, watch_reviews=False, watch_pipelines=False)
    with patch.object(scanner, "_api", side_effect=_api(todos=[TODO])):
        first, state = scanner.poll(config, _state(config, todo_max=555))
    assert first == []
    assert json.loads(state)["todo_ids"] == [555]

    with patch.object(scanner, "_api", side_effect=_api(todos=[])):
        _, state = scanner.poll(config, state)
    with patch.object(scanner, "_api", side_effect=_api(todos=[TODO])):
        pollen, _ = scanner.poll(config, state)
    assert [item["id"] for item in pollen] == ["gitlab-todo-555"]


def test_malformed_provider_fields_and_state_fail_closed(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    pipeline_config = _config(scanner, watch_reviews=False, watch_todos=False)
    state = _state(pipeline_config)
    with patch.object(scanner, "_api", return_value=[{**PIPELINE, "ref": {"bad": "shape"}}]):
        assert scanner.poll(pipeline_config, state) == ([], state)

    todo_config = _config(scanner, watch_reviews=False, watch_pipelines=False)
    todo_state = _state(todo_config, todo_max=554)
    with patch.object(scanner, "_api", return_value=[{**TODO, "author": "alice"}]):
        assert scanner.poll(todo_config, todo_state) == ([], todo_state)

    corrupt = json.loads(_state(todo_config))
    corrupt["todo_max_id"] = "0"
    corrupt_state = json.dumps(corrupt)
    with patch.object(scanner, "_api") as api:
        assert scanner.poll(todo_config, corrupt_state) == ([], corrupt_state)
    api.assert_not_called()


def test_config_types_are_not_silently_coerced(monkeypatch):
    scanner = GitLabScanner()
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    for config in (
        _config(scanner, watch_projects="org/repo"),
        _config(scanner, username={"name": "dhruvil"}),
        _config(scanner, token_env=123),
        _config(scanner, gitlab_url={"url": "https://gitlab.com"}),
    ):
        with patch.object(scanner, "_api") as api:
            assert scanner.poll(config, "safe") == ([], "safe")
        api.assert_not_called()
