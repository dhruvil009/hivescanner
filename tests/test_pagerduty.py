"""Functional tests for PagerDuty's live-set transition scanner."""

import hashlib
import importlib.util
import json
import os
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "pagerduty_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "pagerduty", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
PagerDutyScanner = _mod.PagerDutyScanner


def _incident(incident_id="P123ABC", status="triggered"):
    return {
        "id": incident_id, "status": status, "title": "High CPU on web-1",
        "urgency": "high", "service": {"id": "SVC1", "summary": "web-service"},
        "teams": [{"id": "TEAM1"}],
        "assignments": [{"assignee": {"id": "PUSER1", "summary": "Alice"}}],
        "html_url": f"https://myco.pagerduty.com/incidents/{incident_id}",
        "incident_number": 42, "last_status_change_at": "2026-07-15T10:00:00Z",
    }


def _config(scanner, **changes):
    return {**scanner.configure(), **changes}


def _scope(config):
    return hashlib.sha256(json.dumps({
        "user_id": config["user_id"],
        "team_ids": sorted(config["team_ids"]),
        "service_ids": sorted(config["service_ids"]),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _stored(incident):
    assignment = incident["assignments"][0]["assignee"]
    return {
        "status": incident["status"], "title": incident["title"],
        "urgency": incident["urgency"],
        "service_name": incident["service"]["summary"],
        "html_url": incident["html_url"], "incident_number": incident["incident_number"],
        "assignee_id": assignment["id"], "assignee_name": assignment["summary"],
        "status_changed_at": incident["last_status_change_at"],
    }


def _state(config, incidents=None, cursor=""):
    return json.dumps({
        "version": 3, "initialized": True, "scope": _scope(config),
        "incidents": incidents or {}, "detail_cursor": cursor,
    }, sort_keys=True, separators=(",", ":"))


def test_defaults_bound_active_set_and_detail_calls():
    config = PagerDutyScanner().configure()
    assert config["token_env"] == "PAGERDUTY_TOKEN"
    assert config["max_items"] == 100
    assert config["max_pages"] == 10
    assert PagerDutyScanner.MAX_TRACKED_INCIDENTS == 100
    assert PagerDutyScanner.MAX_DETAIL_CHECKS_PER_POLL == 10


def test_missing_or_invalid_token_env_preserves_watermark(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.delenv("PAGERDUTY_TOKEN", raising=False)
    assert scanner.poll(scanner.configure(), "safe") == ([], "safe")
    assert scanner.poll({"token_env": "BAD-NAME"}, "safe") == ([], "safe")


def test_first_scope_poll_is_quiet_and_snapshots_active_set(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    config = _config(scanner)
    incident = _incident()
    with patch.object(scanner, "_api", return_value={"incidents": [incident], "more": False}):
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    assert json.loads(watermark)["incidents"][incident["id"]] == _stored(incident)


def test_new_triggered_and_acknowledged_transitions_have_distinct_ids(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    config = _config(scanner)
    triggered = _incident()
    with patch.object(scanner, "_api", return_value={"incidents": [triggered], "more": False}):
        pollen, state = scanner.poll(config, _state(config))
    assert [item["type"] for item in pollen] == ["pagerduty_triggered"]
    first_id = pollen[0]["id"]

    acknowledged = {
        **triggered, "status": "acknowledged",
        "last_status_change_at": "2026-07-15T10:05:00Z",
    }
    with patch.object(scanner, "_api", return_value={"incidents": [acknowledged], "more": False}):
        pollen, _ = scanner.poll(config, state)
    assert [item["type"] for item in pollen] == ["pagerduty_acknowledged"]
    assert pollen[0]["id"] != first_id


def test_resolved_missing_incident_is_confirmed_by_detail(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    config = _config(scanner)
    incident = _incident()

    def fake(path, token, params=None):
        if path == "/incidents":
            return {"incidents": [], "more": False}
        return {"incident": {**incident, "status": "resolved"}}

    with patch.object(scanner, "_api", side_effect=fake):
        pollen, watermark = scanner.poll(config, _state(config, {incident["id"]: _stored(incident)}))
    assert [item["type"] for item in pollen] == ["pagerduty_resolved"]
    assert json.loads(watermark)["incidents"] == {}


def test_active_detail_during_list_inconsistency_is_retained_without_false_terminal(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    config = _config(scanner)
    incident = _incident()

    def fake(path, token, params=None):
        return ({"incidents": [], "more": False} if path == "/incidents" else {"incident": incident})

    with patch.object(scanner, "_api", side_effect=fake):
        pollen, watermark = scanner.poll(config, _state(config, {incident["id"]: _stored(incident)}))
    assert pollen == []
    assert incident["id"] in json.loads(watermark)["incidents"]


def test_user_unassignment_and_service_filter_exit_are_proven_by_detail(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    incident = _incident()
    for config, detail, expected in [
        (
            _config(scanner, user_id="PUSER1"),
            {**incident, "assignments": []},
            "pagerduty_unassigned",
        ),
        (
            _config(scanner, service_ids=["SVC1"]),
            {**incident, "service": {"id": "OTHER", "summary": "other"}},
            "pagerduty_no_longer_matching",
        ),
    ]:
        def fake(path, token, params=None, detail=detail):
            return ({"incidents": [], "more": False} if path == "/incidents" else {"incident": detail})

        with patch.object(scanner, "_api", side_effect=fake):
            pollen, _ = scanner.poll(config, _state(config, {incident["id"]: _stored(incident)}))
        assert [item["type"] for item in pollen] == [expected]


def test_detail_cursor_rotates_past_persistent_failures(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    config = _config(scanner)
    incidents = {f"I{i:02d}": _stored(_incident(f"I{i:02d}")) for i in range(12)}
    called = []

    def fake(path, token, params=None):
        if path == "/incidents":
            return {"incidents": [], "more": False}
        called.append(path.rsplit("/", 1)[-1])
        return None

    with patch.object(scanner, "_api", side_effect=fake):
        _, watermark = scanner.poll(config, _state(config, incidents))
        scanner.poll(config, watermark)
    assert called[:10] == [f"I{i:02d}" for i in range(10)]
    assert called[10:12] == ["I10", "I11"]


def test_list_filters_are_passed_as_query_params(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    config = _config(scanner, user_id="PUSER1", team_ids=["TEAM1"], service_ids=["SVC1"])
    with patch.object(scanner, "_api", return_value={"incidents": [], "more": False}) as api:
        scanner.poll(config, _state(config))
    assert api.call_args.args[0] == "/incidents"
    params = api.call_args.args[2]
    assert params["user_ids[]"] == ["PUSER1"]
    assert params["team_ids[]"] == ["TEAM1"]
    assert params["service_ids[]"] == ["SVC1"]


def test_api_error_and_active_set_overflow_fail_closed(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    config = _config(scanner)
    state = _state(config)
    with patch.object(scanner, "_api", return_value=None):
        assert scanner.poll(config, state) == ([], state)
    too_many = [_incident(f"I{i}") for i in range(101)]
    with patch.object(scanner, "_api", return_value={"incidents": too_many, "more": False}):
        assert scanner.poll(config, state) == ([], state)


def test_unknown_list_status_or_malformed_entry_fails_closed(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    config = _config(scanner)
    state = _state(config)
    for incidents in [[_incident(status="snoozed")], ["not-an-incident"]]:
        with patch.object(
            scanner, "_api", return_value={"incidents": incidents, "more": False}
        ):
            assert scanner.poll(config, state) == ([], state)


def test_unknown_detail_status_is_retained_without_false_terminal(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    config = _config(scanner)
    incident = _incident()

    def fake(path, token, params=None):
        if path == "/incidents":
            return {"incidents": [], "more": False}
        return {"incident": {**incident, "status": "snoozed"}}

    with patch.object(scanner, "_api", side_effect=fake):
        pollen, watermark = scanner.poll(
            config, _state(config, {incident["id"]: _stored(incident)})
        )

    assert pollen == []
    assert incident["id"] in json.loads(watermark)["incidents"]


def test_malformed_filter_detail_cannot_become_false_unassignment(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    config = _config(scanner, user_id="PUSER1")
    incident = _incident()

    def fake(path, token, params=None):
        if path == "/incidents":
            return {"incidents": [], "more": False}
        return {"incident": {**incident, "assignments": {"malformed": True}}}

    with patch.object(scanner, "_api", side_effect=fake):
        pollen, watermark = scanner.poll(
            config, _state(config, {incident["id"]: _stored(incident)})
        )
    assert pollen == []
    assert incident["id"] in json.loads(watermark)["incidents"]


def test_malformed_list_contract_and_current_state_fail_closed(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    config = _config(scanner)
    state = _state(config)
    for response in (
        {"incidents": [], "more": "false"},
        {"more": False},
        {"incidents": [{**_incident(), "incident_number": "42"}], "more": False},
    ):
        with patch.object(scanner, "_api", return_value=response):
            assert scanner.poll(config, state) == ([], state)

    corrupt = json.loads(_state(config, {"P123ABC": _stored(_incident())}))
    corrupt["incidents"]["P123ABC"]["urgency"] = {"bad": "shape"}
    corrupt_state = json.dumps(corrupt)
    with patch.object(scanner, "_api", return_value={"incidents": [], "more": False}):
        assert scanner.poll(config, corrupt_state) == ([], corrupt_state)


def test_config_collections_and_json_are_strict(monkeypatch):
    scanner = PagerDutyScanner()
    monkeypatch.setenv("PAGERDUTY_TOKEN", "token")
    for config in (
        _config(scanner, team_ids="TEAM1"),
        _config(scanner, user_id={"id": "PUSER1"}),
        _config(scanner, token_env=123),
    ):
        with patch.object(scanner, "_api") as api:
            assert scanner.poll(config, "safe") == ([], "safe")
        api.assert_not_called()
    try:
        _mod._strict_json('{"more":false,"more":true}')
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate JSON keys must be rejected")
