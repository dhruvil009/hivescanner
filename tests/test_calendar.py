"""Tests for calendar scanner."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

# Insert path BEFORE importing to avoid stdlib calendar conflict
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers", "sources"))

# Import the module under a non-conflicting alias, then grab the class
import importlib
_cal_mod = importlib.import_module("calendar")
CalendarScanner = _cal_mod.CalendarScanner


def _make_event(event_id="evt1", summary="Standup", start_offset_min=15,
                duration_min=30, organizer_email="alice@co.com",
                organizer_name="Alice", updated="2026-03-15T10:00:00Z"):
    """Create a fake calendar event dict."""
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=start_offset_min)
    end = start + timedelta(minutes=duration_min)
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "updated": updated,
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
        "organizer": {"email": organizer_email, "displayName": organizer_name},
    }


_REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name", "group", "url", "metadata",
}


class TestCalendarScanner:

    @patch("calendar.load_snapshot", return_value={})
    def test_configure_returns_expected_defaults(self, mock_load):
        scanner = CalendarScanner()
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["reminder_minutes"] == [30, 10]
        assert config["max_events"] == 20
        assert config["calendars"] == []

    @patch("calendar.save_snapshot")
    @patch("calendar.load_snapshot", return_value={})
    def test_poll_returns_empty_when_gws_not_installed(self, mock_load, mock_save):
        scanner = CalendarScanner()
        with patch("shutil.which", return_value=None):
            pollen, wm = scanner.poll(scanner.configure(), "")
        assert pollen == []

    @patch("calendar.save_snapshot")
    @patch("calendar.load_snapshot", return_value={})
    def test_poll_returns_empty_when_no_events(self, mock_load, mock_save):
        scanner = CalendarScanner()
        scanner._bootstrapped = True
        with patch("shutil.which", return_value="/usr/bin/gws"), \
             patch.object(scanner, "_gws", return_value="[]"):
            pollen, wm = scanner.poll(scanner.configure(), "")
        assert pollen == []

    @patch("calendar.save_snapshot")
    @patch("calendar.load_snapshot", return_value={})
    def test_meeting_reminder_emitted_within_window(self, mock_load, mock_save):
        scanner = CalendarScanner()
        scanner._bootstrapped = True
        event = _make_event(start_offset_min=8)  # 8 min from now, within 10-min window
        raw_json = json.dumps([event])

        with patch("shutil.which", return_value="/usr/bin/gws"), \
             patch.object(scanner, "_gws", return_value=raw_json):
            config = scanner.configure()
            pollen, wm = scanner.poll(config, "")

        reminders = [p for p in pollen if p["type"] == "meeting_reminder"]
        assert len(reminders) >= 1
        # Should have a reminder for the 10-min window (since event is 8 min away)
        ids = [p["id"] for p in reminders]
        assert any("calendar-reminder-evt1-10" in pid for pid in ids)

    @patch("calendar.save_snapshot")
    @patch("calendar.load_snapshot", return_value={})
    def test_bootstrap_silence_first_poll_emits_no_pollen(self, mock_load, mock_save):
        """First poll should snapshot events without emitting any pollen."""
        scanner = CalendarScanner()
        # _bootstrapped is False because load_snapshot returned {}
        assert scanner._bootstrapped is False
        event = _make_event(start_offset_min=5)
        raw_json = json.dumps([event])

        with patch("shutil.which", return_value="/usr/bin/gws"), \
             patch.object(scanner, "_gws", return_value=raw_json):
            pollen, wm = scanner.poll(scanner.configure(), "")

        assert pollen == []
        # After first poll, bootstrapped should be True
        assert scanner._bootstrapped is True

    @patch("calendar.save_snapshot")
    @patch("calendar.load_snapshot", return_value={})
    def test_pollen_schema_has_all_required_keys(self, mock_load, mock_save):
        scanner = CalendarScanner()
        scanner._bootstrapped = True
        event = _make_event(start_offset_min=5)
        raw_json = json.dumps([event])

        with patch("shutil.which", return_value="/usr/bin/gws"), \
             patch.object(scanner, "_gws", return_value=raw_json):
            pollen, wm = scanner.poll(scanner.configure(), "")

        assert len(pollen) > 0, "Expected at least one pollen item"
        for item in pollen:
            missing = _REQUIRED_POLLEN_KEYS - set(item.keys())
            assert not missing, f"Pollen missing keys: {missing}"

    @patch("calendar.save_snapshot")
    @patch("calendar.load_snapshot", return_value={})
    def test_event_changed_detection(self, mock_load, mock_save):
        scanner = CalendarScanner()
        scanner._bootstrapped = True
        # Pre-seed snapshot with different value to trigger change detection
        scanner._event_snapshot = {
            "evt1": "Old Title|2026-03-15T10:00:00+00:00|2026-03-15T11:00:00+00:00|2026-03-15T09:00:00Z"
        }
        event = _make_event(start_offset_min=120)  # far in future so no reminder
        raw_json = json.dumps([event])

        with patch("shutil.which", return_value="/usr/bin/gws"), \
             patch.object(scanner, "_gws", return_value=raw_json):
            pollen, wm = scanner.poll(scanner.configure(), "")

        changed = [p for p in pollen if p["type"] == "event_changed"]
        assert len(changed) == 1
        assert "calendar-changed-evt1" == changed[0]["id"]
