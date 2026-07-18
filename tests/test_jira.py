"""Functional tests for Jira Cloud enhanced JQL and ADF transitions."""

import hashlib
import importlib.util
import json
import os
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "jira_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "jira", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
JiraScanner = _mod.JiraScanner

ACCOUNT = "5b10ac8d82e05b22cc7d4ef5"


def _issue(
    *,
    key="PROJ-123",
    status="In Progress",
    assignee="other-account",
    description=None,
    updated="2026-07-15T10:00:00.000+0000",
):
    return {
        "key": key,
        "fields": {
            "summary": "Fix login bug",
            "status": {"name": status},
            "priority": {"name": "High"},
            "issuetype": {"name": "Bug"},
            "assignee": {"accountId": assignee, "displayName": "Assignee"},
            "creator": {"accountId": "creator", "displayName": "Creator"},
            "description": description or {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Broken"}]}],
            },
            "updated": updated,
        },
    }


def _config(scanner, **changes):
    return {
        **scanner.configure(),
        "domain": "myco.atlassian.net",
        "username": "alice@co.com",
        "account_id": ACCOUNT,
        **changes,
    }


def _scope(config):
    value = {
        "domain": config["domain"],
        "username": config["username"],
        "jql": config["jql"],
        "account_id": config["account_id"],
        "mention_terms": sorted(value.casefold() for value in config["mention_terms"]),
        "jira_timezone": config["jira_timezone"],
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _current(issue):
    fields = issue["fields"]
    description = _mod._adf_to_text(fields["description"])
    mentioned = _mod._adf_mentions_account(fields["description"], ACCOUNT)
    return {
        "updated": fields["updated"],
        "assignee_id": fields["assignee"]["accountId"],
        "mentioned": mentioned,
        "description_hash": hashlib.sha256(description.encode()).hexdigest()[:16],
        "status": fields["status"]["name"],
    }


def _state(config, issues=None, **extra):
    state = {
        "version": 4,
        "updated_at": "2026-07-15T09:00:00Z",
        "initialized": True,
        "scope": _scope(config),
        "issues": issues or {},
        "next_page_token": "",
        "pending_since_jql": "",
        "pending_until": "",
    }
    state.update(extra)
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def test_defaults_use_current_enhanced_search_controls():
    config = JiraScanner().configure()
    assert config["token_env"] == "JIRA_TOKEN"
    assert config["max_items"] == 100
    assert config["max_pages"] == 10
    assert config["overlap_minutes"] == 10
    assert config["jira_timezone"] == "UTC"


def test_missing_credentials_or_invalid_domain_preserves_watermark(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    assert scanner.poll(_config(scanner), "safe") == ([], "safe")
    monkeypatch.setenv("JIRA_TOKEN", "token")
    for domain in [
        "",
        "user:pass@host",
        "host/path",
        "host:bad",
        "jira.attacker.example",
        "atlassian.net.attacker.example",
    ]:
        assert scanner.poll(_config(scanner, domain=domain), "safe") == ([], "safe")


def test_first_scope_poll_is_quiet_and_uses_bounded_one_day_jql(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner)
    with patch.object(
        scanner,
        "_api",
        return_value={"issues": [_issue()], "isLast": True},
    ) as api:
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    assert json.loads(watermark)["issues"]["PROJ-123"]
    assert api.call_args.args[0] == "search/jql"
    jql = api.call_args.args[4]["jql"]
    assert "updated >= -1d" in jql and "updated <=" in jql
    assert jql.endswith("ORDER BY updated DESC")


def test_assignment_transition_is_detected_by_account_id(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner)
    previous_issue = _issue(assignee="someone-else", updated="2026-07-15T09:30:00Z")
    current = _issue(assignee=ACCOUNT)
    state = _state(config, {"PROJ-123": _current(previous_issue)})
    with patch.object(scanner, "_api", return_value={"issues": [current], "isLast": True}):
        pollen, _ = scanner.poll(config, state)
    assert [item["type"] for item in pollen] == ["jira_assigned"]
    assert pollen[0]["metadata"]["issue_key"] == "PROJ-123"


def test_new_issue_currently_assigned_is_classified_as_assigned(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner)
    with patch.object(
        scanner,
        "_api",
        return_value={"issues": [_issue(assignee=ACCOUNT)], "isLast": True},
    ):
        pollen, _ = scanner.poll(config, _state(config))
    assert [item["type"] for item in pollen] == ["jira_assigned"]


def test_exact_adf_mention_transition_is_detected(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner)
    prior = _issue(updated="2026-07-15T09:30:00Z")
    mention = {
        "type": "doc",
        "content": [{
            "type": "paragraph",
            "content": [{"type": "mention", "attrs": {"id": ACCOUNT, "text": "@alice"}}],
        }],
    }
    current = _issue(description=mention)
    with patch.object(scanner, "_api", return_value={"issues": [current], "isLast": True}):
        pollen, _ = scanner.poll(config, _state(config, {"PROJ-123": _current(prior)}))
    assert [item["type"] for item in pollen] == ["jira_mentioned"]


def test_configured_text_terms_use_boundaries_not_substrings(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner, account_id="", mention_terms=["ann"])
    prior = _issue(description="plain", updated="2026-07-15T09:30:00Z")
    planning = _issue(description="planning", updated="2026-07-15T10:00:00Z")
    with patch.object(scanner, "_api", return_value={"issues": [planning], "isLast": True}):
        pollen, _ = scanner.poll(config, _state(config, {"PROJ-123": {
            **_current(prior), "mentioned": False,
        }}))
    assert [item["type"] for item in pollen] == ["jira_updated"]


def test_status_transitions_have_distinct_ids(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner)
    previous = _issue(status="Todo", updated="2026-07-15T09:00:00Z")
    ids = []
    state = _state(config, {"PROJ-123": _current(previous)})
    for index, status in enumerate(["In Progress", "Done"], start=1):
        current = _issue(status=status, updated=f"2026-07-15T1{index}:00:00Z")
        with patch.object(scanner, "_api", return_value={"issues": [current], "isLast": True}):
            pollen, state = scanner.poll(config, state)
        ids.append(pollen[0]["id"])
    assert len(set(ids)) == 2


def test_enhanced_search_continuation_keeps_fixed_window(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner, max_pages=1)
    initial = _state(config)
    with patch.object(
        scanner,
        "_api",
        return_value={"issues": [_issue()], "nextPageToken": "cursor", "isLast": False},
    ) as api:
        _, continuation = scanner.poll(config, initial)
    mid = json.loads(continuation)
    assert mid["next_page_token"] == "cursor"
    assert mid["updated_at"] == "2026-07-15T09:00:00Z"
    first_jql = api.call_args.args[4]["jql"]
    assert first_jql.endswith("ORDER BY updated ASC")

    with patch.object(
        scanner,
        "_api",
        return_value={"issues": [], "isLast": True},
    ) as api:
        _, finished = scanner.poll(config, continuation)
    assert api.call_args.args[4]["nextPageToken"] == "cursor"
    assert api.call_args.args[4]["jql"] == first_jql
    assert json.loads(finished)["next_page_token"] == ""


def test_api_error_and_invalid_timezone_preserve_watermark(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner)
    state = _state(config)
    with patch.object(scanner, "_api", return_value=None):
        assert scanner.poll(config, state) == ([], state)
    assert scanner.poll(_config(scanner, jira_timezone="Not/AZone"), state) == ([], state)


def test_non_string_mention_term_fails_before_api(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    with patch.object(scanner, "_api") as api:
        assert scanner.poll(
            _config(scanner, mention_terms=["alice", {"term": "bob"}]),
            "safe",
        ) == ([], "safe")
    api.assert_not_called()


def test_malformed_issue_or_pagination_contract_preserves_state(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner)
    state = _state(config)
    malformed_results = [
        {"issues": ["not-an-issue"], "isLast": True},
        {"issues": [{"key": "PROJ-1"}], "isLast": True},
        {"issues": [{**_issue(), "fields": {}}], "isLast": True},
        {"issues": [], "isLast": False},
        {"issues": []},
    ]
    for result in malformed_results:
        with patch.object(scanner, "_api", return_value=result):
            assert scanner.poll(config, state) == ([], state)


def test_malformed_assignee_cannot_create_a_later_false_assignment(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner)
    assigned = _issue(assignee=ACCOUNT, updated="2026-07-15T09:30:00Z")
    state = _state(config, {"PROJ-123": _current(assigned)})
    malformed = _issue(assignee=ACCOUNT)
    malformed["fields"]["assignee"] = {"accountId": {"bad": "shape"}}
    with patch.object(
        scanner, "_api", return_value={"issues": [malformed], "isLast": True}
    ):
        assert scanner.poll(config, state) == ([], state)


def test_repeated_or_contradictory_page_token_fails_closed(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner, max_pages=2)
    state = _state(config)
    for result in (
        {"issues": [], "nextPageToken": "next", "isLast": True},
        {"issues": [], "nextPageToken": "repeat", "isLast": False},
    ):
        if result["nextPageToken"] == "repeat":
            continuation = _state(
                config,
                next_page_token="repeat",
                pending_since_jql="2026-07-15 08:50",
                pending_until="2026-07-15T10:00:00Z",
            )
        else:
            continuation = state
        with patch.object(scanner, "_api", return_value=result):
            assert scanner.poll(config, continuation) == ([], continuation)


def test_invalid_current_state_and_config_types_fail_before_api(monkeypatch):
    scanner = JiraScanner()
    monkeypatch.setenv("JIRA_TOKEN", "token")
    config = _config(scanner)
    corrupt = json.loads(_state(config))
    corrupt["issues"] = {"PROJ-1": {"updated": "not-a-date"}}
    corrupt_state = json.dumps(corrupt)
    with patch.object(scanner, "_api") as api:
        assert scanner.poll(config, corrupt_state) == ([], corrupt_state)
    api.assert_not_called()

    for invalid in (
        _config(scanner, mention_terms="alice"),
        _config(scanner, jql={"query": "assignee=currentUser()"}),
        _config(scanner, username={"email": "alice@co.com"}),
        _config(scanner, token_env=123),
    ):
        with patch.object(scanner, "_api") as api:
            assert scanner.poll(invalid, "safe") == ([], "safe")
        api.assert_not_called()
