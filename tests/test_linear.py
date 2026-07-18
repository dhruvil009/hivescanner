"""Functional tests for Linear cursor pagination and stable time windows."""

import hashlib
import importlib.util
import json
import os
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "linear_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "linear", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
LinearScanner = _mod.LinearScanner


def _node(
    *, state="In Progress", updated="2026-07-15T10:00:00Z",
    created="2026-07-15T09:30:00Z", assignee_id="user-1",
):
    return {
        "id": "uuid-001", "identifier": "ENG-101", "title": "Fix login bug",
        "state": {"id": "state-1", "name": state}, "priority": 1,
        "assignee": {"id": assignee_id, "name": "Alice", "email": "alice@example.com"},
        "creator": {"id": "creator", "name": "Bob", "email": "bob@example.com"},
        "createdAt": created, "updatedAt": updated,
        "url": "https://linear.app/team/issue/ENG-101",
    }


def _current(node):
    return {
        "state": node["state"]["name"], "priority": node["priority"],
        "assignee_id": node["assignee"]["id"], "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
    }


def _response(nodes, *, has_next=False, cursor=None):
    return {"data": {"issues": {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }}}


def _config(scanner, **changes):
    return {**scanner.configure(), **changes}


def _scope(config):
    return hashlib.sha256(json.dumps(
        {"team_id": config["team_id"], "assignee_id": config["assignee_id"]},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()[:16]


def _state(config, issues=None, **extra):
    state = {
        "version": 3, "updated_at": "2026-07-15T09:00:00Z",
        "initialized": True, "scope": _scope(config), "issues": issues or {},
        "cursor": "", "pending_until": "", "pending_since": "",
    }
    state.update(extra)
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def test_defaults_match_linear_page_limits():
    config = LinearScanner().configure()
    assert config["api_key_env"] == "LINEAR_API_KEY"
    assert config["max_items"] == 50
    assert config["max_pages"] == 10
    assert config["overlap_minutes"] == 5


def test_missing_or_invalid_api_key_env_preserves_watermark(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    assert scanner.poll(scanner.configure(), "safe") == ([], "safe")
    assert scanner.poll({"api_key_env": "BAD-NAME"}, "safe") == ([], "safe")


def test_first_scope_poll_is_quiet_and_bounded_to_one_page(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner)
    with patch.object(scanner, "_graphql", return_value=_response(
        [_node()], has_next=True, cursor="ignored-history",
    )) as graphql:
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    state = json.loads(watermark)
    expected = _current(_node())
    expected["created_at"] = _mod._canonical_timestamp(expected["created_at"])
    expected["updated_at"] = _mod._canonical_timestamp(expected["updated_at"])
    assert state["issues"]["ENG-101"] == expected
    assert state["cursor"] == ""
    assert graphql.call_count == 1


def test_new_issue_without_assignee_scope_is_classified_new(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner)
    with patch.object(scanner, "_graphql", return_value=_response([_node()])):
        pollen, _ = scanner.poll(config, _state(config))
    assert [item["type"] for item in pollen] == ["linear_issue_new"]
    assert pollen[0]["author"] == "bob@example.com"


def test_new_or_transitioned_assignment_uses_configured_assignee_id(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner, assignee_id="user-1")
    with patch.object(scanner, "_graphql", return_value=_response([_node()])):
        pollen, _ = scanner.poll(config, _state(config))
    assert [item["type"] for item in pollen] == ["issue_assigned"]

    prior = _node(assignee_id="someone-else", updated="2026-07-15T09:30:00Z")
    with patch.object(scanner, "_graphql", return_value=_response([_node()])):
        pollen, _ = scanner.poll(config, _state(config, {"ENG-101": _current(prior)}))
    assert [item["type"] for item in pollen] == ["issue_assigned"]


def test_state_changes_emit_distinct_transition_ids(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner)
    todo = _node(state="Todo", updated="2026-07-15T09:30:00Z")
    state = _state(config, {"ENG-101": _current(todo)})
    ids = []
    for node in [
        _node(state="In Progress", updated="2026-07-15T10:00:00Z"),
        _node(state="Done", updated="2026-07-15T11:00:00Z"),
    ]:
        with patch.object(scanner, "_graphql", return_value=_response([node])):
            pollen, state = scanner.poll(config, state)
        ids.append(pollen[0]["id"])
    assert len(set(ids)) == 2
    assert all(value.startswith("linear-ENG-101-") for value in ids)


def test_unchanged_issue_is_suppressed(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner)
    node = _node()
    with patch.object(scanner, "_graphql", return_value=_response([node])):
        pollen, _ = scanner.poll(config, _state(config, {"ENG-101": _current(node)}))
    assert pollen == []


def test_filter_variables_include_fixed_since_until_and_scope(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner, team_id="team-1", assignee_id="user-1")
    with patch.object(scanner, "_graphql", return_value=_response([])) as graphql:
        scanner.poll(config, _state(config))
    query, variables, _ = graphql.call_args.args
    assert "updatedAt: { lte: $until, gte: $since }" in query
    assert "team: { id: { eq: $teamId } }" in query
    assert "assignee: { id: { eq: $assigneeId } }" in query
    assert variables["since"] == "2026-07-15T08:55:00Z"
    assert variables["teamId"] == "team-1"


def test_cursor_continuation_preserves_window_until_last_page(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner, max_pages=1)
    with patch.object(scanner, "_graphql", return_value=_response(
        [_node()], has_next=True, cursor="cursor-1",
    )):
        _, continuation = scanner.poll(config, _state(config))
    mid = json.loads(continuation)
    assert mid["cursor"] == "cursor-1"
    assert mid["updated_at"] == "2026-07-15T09:00:00.000000Z"
    fixed_until = mid["pending_until"]

    with patch.object(scanner, "_graphql", return_value=_response([])) as graphql:
        _, finished = scanner.poll(config, continuation)
    variables = graphql.call_args.args[1]
    assert variables["after"] == "cursor-1"
    assert variables["until"] == _mod._canonical_timestamp(fixed_until)
    assert json.loads(finished)["cursor"] == ""


def test_graphql_error_and_malformed_shape_fail_closed(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner)
    state = _state(config)
    for response in [None, {"data": None}, {"data": {"issues": []}}]:
        with patch.object(scanner, "_graphql", return_value=response):
            assert scanner.poll(config, state) == ([], state)


def test_malformed_nodes_and_pagination_contract_do_not_advance(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner)
    state = _state(config)
    responses = [
        _response(["not-a-node"]),
        _response([{**_node(), "updatedAt": None}]),
        {"data": {"issues": {"nodes": [], "pageInfo": {}}}},
        {
            "data": {
                "issues": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": None},
                }
            }
        },
    ]
    for response in responses:
        with patch.object(scanner, "_graphql", return_value=response):
            assert scanner.poll(config, state) == ([], state)


def test_new_issue_classification_compares_instants_not_timestamp_text(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner)
    node = _node(created="2026-07-15T02:30:00-07:00")
    with patch.object(scanner, "_graphql", return_value=_response([node])):
        pollen, _ = scanner.poll(config, _state(config))
    assert [item["type"] for item in pollen] == ["linear_issue_new"]


def test_malformed_nested_node_and_repeated_cursor_fail_closed(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner)
    state = _state(config)
    malformed = _node()
    malformed["assignee"] = {"id": {"bad": "shape"}}
    with patch.object(scanner, "_graphql", return_value=_response([malformed])):
        assert scanner.poll(config, state) == ([], state)

    continuation = _state(
        config,
        cursor="repeat",
        pending_until="2026-07-15T10:00:00Z",
        pending_since="2026-07-15T08:55:00Z",
    )
    with patch.object(
        scanner,
        "_graphql",
        return_value=_response([], has_next=True, cursor="repeat"),
    ):
        assert scanner.poll(config, continuation) == ([], continuation)


def test_invalid_state_and_config_types_fail_before_graphql(monkeypatch):
    scanner = LinearScanner()
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    config = _config(scanner)
    corrupt = json.loads(_state(config))
    corrupt["issues"] = {"ENG-1": {"priority": "high"}}
    corrupt_state = json.dumps(corrupt)
    with patch.object(scanner, "_graphql") as graphql:
        assert scanner.poll(config, corrupt_state) == ([], corrupt_state)
    graphql.assert_not_called()

    for invalid in (
        _config(scanner, team_id={"id": "team-1"}),
        _config(scanner, assignee_id=["user-1"]),
        _config(scanner, api_key_env=123),
    ):
        with patch.object(scanner, "_graphql") as graphql:
            assert scanner.poll(invalid, "safe") == ([], "safe")
        graphql.assert_not_called()
