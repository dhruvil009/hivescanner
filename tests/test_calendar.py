"""Functional tests for the Google Calendar scanner."""

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


_CAL_PATH = os.path.join(
    os.path.dirname(__file__), "..", "workers", "sources", "calendar.py"
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
_spec = importlib.util.spec_from_file_location("calendar_scanner", _CAL_PATH)
_cal_mod = importlib.util.module_from_spec(_spec)
sys.modules["calendar_scanner"] = _cal_mod
_spec.loader.exec_module(_cal_mod)
CalendarScanner = _cal_mod.CalendarScanner


def _scanner(*, snapshot_exists=False):
    with (
        patch.object(_cal_mod, "load_snapshot", return_value={}),
        patch.object(_cal_mod, "snapshot_exists", return_value=snapshot_exists),
    ):
        scanner = CalendarScanner()
    scanner._cli_available = True
    if snapshot_exists:
        scanner._event_snapshot = {
            "schema_version": 2,
            "committed": {},
            "candidate": {},
            "candidate_watermark": "1970-01-01T00:00:00Z",
            "bootstrap_pending": False,
        }
        scanner._reminded_snapshot = {
            "schema_version": 2,
            "committed": {},
            "candidate": {},
            "candidate_watermark": "1970-01-01T00:00:00Z",
        }
    return scanner


def _make_event(
    event_id="evt1",
    summary="Standup",
    start_offset_min=15,
    duration_min=30,
    updated=None,
):
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=start_offset_min)
    end = start + timedelta(minutes=duration_min)
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "updated": updated or now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
        "organizer": {"email": "alice@co.com", "displayName": "Alice"},
    }


def _poll(scanner, config, events, watermark=""):
    responses = [
        json.dumps({"items": [{"id": "primary"}]}),
        json.dumps({"items": events}),
    ]
    with (
        patch.object(scanner, "_gws", side_effect=responses),
        patch.object(_cal_mod, "save_snapshot"),
    ):
        return scanner.poll(config, watermark)


REQUIRED_KEYS = {
    "id", "source", "type", "title", "preview", "discovered_at",
    "author", "author_name", "group", "url", "metadata",
}


def test_configure_matches_bounded_real_world_defaults():
    config = _scanner().configure()
    assert config["enabled"] is False
    assert config["reminder_minutes"] == [30, 10]
    assert config["max_events"] == 1000
    assert config["max_pages"] == 10
    assert config["lookahead_days"] == 30
    assert config["calendars"] == []


def test_missing_cli_preserves_watermark():
    scanner = _scanner()
    scanner._cli_available = False
    assert scanner.poll(scanner.configure(), "committed") == ([], "committed")


def test_first_poll_is_quiet_and_stages_snapshot_until_watermark_commit():
    scanner = _scanner()
    event = _make_event(start_offset_min=5)
    pollen, watermark = _poll(scanner, scanner.configure(), [event])

    assert pollen == []
    assert scanner._event_snapshot["bootstrap_pending"] is True
    assert "primary\0evt1" in scanner._event_snapshot["candidate"]

    # Feeding the returned watermark back models the scanner loop committing
    # the durable pollen batch. Only then is bootstrap complete.
    pollen, _ = _poll(scanner, scanner.configure(), [event], watermark)
    assert scanner._event_snapshot["bootstrap_pending"] is False
    assert any(item["type"] == "meeting_reminder" for item in pollen)


def test_reminder_is_bound_to_calendar_event_start_and_not_raw_external_id():
    scanner = _scanner(snapshot_exists=True)
    event = _make_event(start_offset_min=8)
    pollen, _ = _poll(scanner, scanner.configure(), [event], "2026-07-15T00:00:00Z")

    reminder = next(item for item in pollen if item["type"] == "meeting_reminder")
    assert reminder["metadata"]["event_id"] == "evt1"
    assert reminder["metadata"]["calendar_id"] == "primary"
    assert reminder["metadata"]["reminder_minutes"] == 10
    assert reminder["id"].startswith("calendar-reminder-")
    assert "evt1" not in reminder["id"]
    assert REQUIRED_KEYS <= reminder.keys()


def test_committed_event_change_emits_once_with_new_shape():
    scanner = _scanner()
    original = _make_event(start_offset_min=120, summary="Old title")
    pollen, watermark = _poll(scanner, scanner.configure(), [original])
    assert pollen == []

    changed = dict(original)
    changed["summary"] = "New title"
    changed["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pollen, _ = _poll(scanner, scanner.configure(), [changed], watermark)

    updates = [item for item in pollen if item["type"] == "event_changed"]
    assert len(updates) == 1
    assert updates[0]["title"] == "Event updated: New title"
    assert updates[0]["metadata"]["event_id"] == "evt1"


def test_disappearing_event_does_not_create_false_cancellation():
    scanner = _scanner()
    event = _make_event(start_offset_min=120)
    _, watermark = _poll(scanner, scanner.configure(), [event])
    pollen, _ = _poll(scanner, scanner.configure(), [], watermark)
    assert pollen == []


def test_explicit_cancelled_event_is_reported():
    scanner = _scanner()
    event = _make_event(start_offset_min=120)
    _, watermark = _poll(scanner, scanner.configure(), [event])
    cancelled = {**event, "status": "cancelled", "updated": datetime.now(timezone.utc).isoformat()}
    pollen, _ = _poll(scanner, scanner.configure(), [cancelled], watermark)
    assert [item["type"] for item in pollen] == ["event_changed"]
    assert pollen[0]["title"].startswith("Event cancelled:")


def test_calendar_qualified_keys_prevent_cross_calendar_collisions():
    scanner = _scanner(snapshot_exists=True)
    config = {**scanner.configure(), "calendars": ["a@example.com", "b@example.com"]}
    event_a = _make_event(start_offset_min=120)
    event_b = _make_event(start_offset_min=180)
    responses = [
        json.dumps({"items": [event_a]}),
        json.dumps({"items": [event_b]}),
    ]
    with (
        patch.object(scanner, "_gws", side_effect=responses),
        patch.object(_cal_mod, "save_snapshot"),
    ):
        scanner.poll(config, "2026-07-15T00:00:00Z")
    keys = scanner._event_snapshot["candidate"]
    assert set(keys) == {"a@example.com\0evt1", "b@example.com\0evt1"}


def test_over_limit_event_list_fails_closed_and_preserves_watermark():
    scanner = _scanner(snapshot_exists=True)
    config = {**scanner.configure(), "max_events": 1}
    events = [_make_event("a"), _make_event("b")]
    assert _poll(scanner, config, events, "safe-watermark") == ([], "safe-watermark")


def test_uses_generated_calendar_api_surface_with_full_event_fields():
    scanner = _scanner(snapshot_exists=True)
    config = {**scanner.configure(), "calendars": ["primary"], "timezone": "UTC"}
    calls = []

    def fake_gws(args):
        calls.append(args)
        return json.dumps({"items": [_make_event()]})

    with (
        patch.object(scanner, "_gws", side_effect=fake_gws),
        patch.object(_cal_mod, "save_snapshot"),
    ):
        scanner.poll(config, "2026-07-15T00:00:00Z")

    assert calls[0][:3] == ["calendar", "events", "list"]
    assert "+agenda" not in calls[0]
    params = json.loads(calls[0][calls[0].index("--params") + 1])
    assert params["calendarId"] == "primary"
    assert params["singleEvents"] is True
    assert params["showDeleted"] is True
    assert params["orderBy"] == "startTime"
    assert params["timeZone"] == "UTC"
    assert params["timeMin"].endswith("Z")
    assert params["timeMax"].endswith("Z")


def test_discovers_calendars_and_paginates_generated_api_responses():
    scanner = _scanner(snapshot_exists=True)
    config = {**scanner.configure(), "max_pages": 2}
    responses = [
        json.dumps({"items": [{"id": "primary"}]}),
        json.dumps({"items": [_make_event("a")], "nextPageToken": "page-2"}),
        json.dumps({"items": [_make_event("b")]}),
    ]
    calls = []

    def fake_gws(args):
        calls.append(args)
        return responses.pop(0)

    with (
        patch.object(scanner, "_gws", side_effect=fake_gws),
        patch.object(_cal_mod, "save_snapshot"),
    ):
        scanner.poll(config, "2026-07-15T00:00:00Z")

    assert calls[0][:3] == ["calendar", "calendarList", "list"]
    assert calls[1][:3] == ["calendar", "events", "list"]
    second_page_params = json.loads(calls[2][calls[2].index("--params") + 1])
    assert second_page_params["pageToken"] == "page-2"
    assert set(scanner._event_snapshot["candidate"]) == {
        "primary\0a",
        "primary\0b",
    }


def test_malformed_or_truncated_generated_response_preserves_state():
    scanner = _scanner(snapshot_exists=True)
    config = {**scanner.configure(), "calendars": ["primary"]}
    with (
        patch.object(scanner, "_gws", return_value=json.dumps({"events": []})),
        patch.object(_cal_mod, "save_snapshot") as save,
    ):
        result = scanner.poll(config, "safe-watermark")

    assert result == ([], "safe-watermark")
    save.assert_not_called()


def test_pagination_overflow_preserves_state_instead_of_committing_partial_view():
    scanner = _scanner(snapshot_exists=True)
    config = {**scanner.configure(), "calendars": ["primary"], "max_pages": 1}
    response = json.dumps({"items": [_make_event()], "nextPageToken": "more"})
    with (
        patch.object(scanner, "_gws", return_value=response),
        patch.object(_cal_mod, "save_snapshot") as save,
    ):
        result = scanner.poll(config, "safe-watermark")

    assert result == ([], "safe-watermark")
    save.assert_not_called()


def test_control_characters_are_rejected_before_cli_invocation():
    scanner = _scanner(snapshot_exists=True)
    config = {**scanner.configure(), "calendars": ["primary\n--format=yaml"]}
    with patch.object(scanner, "_gws") as gws:
        assert scanner.poll(config, "safe-watermark") == ([], "safe-watermark")
    gws.assert_not_called()


def test_invalid_current_snapshots_and_config_fail_before_cli_invocation():
    scanner = _scanner(snapshot_exists=True)
    scanner._event_snapshot["bootstrap_pending"] = "false"
    watermark = "2026-07-15T00:00:00Z"
    with patch.object(scanner, "_gws") as gws:
        assert scanner.poll(scanner.configure(), watermark) == ([], watermark)
    gws.assert_not_called()

    scanner = _scanner(snapshot_exists=True)
    scanner._reminded_snapshot["committed"] = {"malformed": "not-a-time"}
    with patch.object(scanner, "_gws") as gws:
        assert scanner.poll(scanner.configure(), watermark) == ([], watermark)
    gws.assert_not_called()

    scanner = _scanner(snapshot_exists=True)
    invalid_configs = [
        {**scanner.configure(), "noise_subjects": "Lunch"},
        {**scanner.configure(), "calendars": ["primary", "primary"]},
        {**scanner.configure(), "reminder_minutes": [10, 10]},
        {**scanner.configure(), "timezone": " UTC"},
    ]
    with patch.object(scanner, "_gws") as gws:
        for config in invalid_configs:
            assert scanner.poll(config, watermark) == ([], watermark)
    gws.assert_not_called()


def test_legacy_snapshot_migrates_with_one_quiet_bootstrap():
    scanner = _scanner(snapshot_exists=True)
    scanner._event_snapshot = {"evt1": "old|ambiguous|snapshot|value"}
    scanner._reminded_snapshot = {"evt1-10": "2026-07-15T00:00:00Z"}
    changed = _make_event(summary="Changed during upgrade", start_offset_min=5)
    pollen, _ = _poll(
        scanner,
        scanner.configure(),
        [changed],
        "2026-07-15T00:00:00Z",
    )
    assert pollen == []
    assert scanner._event_snapshot["bootstrap_pending"] is True


def test_duplicate_json_error_wrapper_and_repeated_cursor_fail_closed():
    scanner = _scanner(snapshot_exists=True)
    config = {**scanner.configure(), "calendars": ["primary"], "max_pages": 2}
    watermark = "2026-07-15T00:00:00Z"
    invalid_responses = [
        '{"items":[],"items":[]}',
        json.dumps({"error": {"code": 500}}),
    ]
    for response in invalid_responses:
        with (
            patch.object(scanner, "_gws", return_value=response),
            patch.object(_cal_mod, "save_snapshot") as save,
        ):
            assert scanner.poll(config, watermark) == ([], watermark)
        save.assert_not_called()

    repeated = json.dumps({"items": [], "nextPageToken": "same"})
    with (
        patch.object(scanner, "_gws", side_effect=[repeated, repeated]),
        patch.object(_cal_mod, "save_snapshot") as save,
    ):
        assert scanner.poll(config, watermark) == ([], watermark)
    save.assert_not_called()


def test_gws_environment_drops_response_mutating_overrides():
    scanner = _scanner()
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        kwargs["stdout"].write(b"{}")
        return subprocess.CompletedProcess(args[0], 0)

    hostile = {
        "GOOGLE_WORKSPACE_CLI_SANITIZE_TEMPLATE": "projects/attacker/templates/x",
        "GOOGLE_WORKSPACE_CLI_SANITIZE_MODE": "sanitize",
        "GOOGLE_WORKSPACE_CLI_LOG_FILE": "/tmp/leak",
        "GOOGLE_APPLICATION_CREDENTIALS": "/safe/auth.json",
    }
    with (
        patch.dict(os.environ, hostile, clear=True),
        patch.object(_cal_mod.subprocess, "run", side_effect=fake_run),
    ):
        assert scanner._gws(["calendar", "calendarList", "list"]) == "{}"

    assert captured["GOOGLE_APPLICATION_CREDENTIALS"] == "/safe/auth.json"
    assert not any("SANITIZE" in key or key.endswith("LOG_FILE") for key in captured)
