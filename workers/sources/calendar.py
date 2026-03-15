"""Calendar scanner — surfaces meeting reminders via `gws` CLI (Google Workspace)."""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone

# Resolve imports whether run as module or standalone
try:
    from snapshot_store import load_snapshot, save_snapshot
except ImportError:
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from snapshot_store import load_snapshot, save_snapshot


class CalendarScanner:
    name = "calendar"

    def __init__(self):
        self._cli_available = None
        self._event_snapshot = load_snapshot("calendar_events")
        self._reminded_snapshot = load_snapshot("calendar_reminded")
        self._bootstrapped = bool(self._event_snapshot)

    def configure(self) -> dict:
        return {
            "enabled": False,
            "reminder_minutes": [30, 10],
            "max_events": 20,
            "calendars": [],
        }

    def _gws(self, args: list[str], timeout: int = 15) -> str | None:
        """Run gws CLI command, return stdout or None on failure."""
        try:
            result = subprocess.run(
                ["gws"] + args,
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                print(f"[calendar] gws error: {result.stderr[:200]}", file=sys.stderr)
                return None
            return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"[calendar] gws failed: {e}", file=sys.stderr)
            return None

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        if self._cli_available is None:
            self._cli_available = shutil.which("gws") is not None
        if not self._cli_available:
            return [], watermark

        raw = self._gws(["calendar", "+agenda", "--output", "json"])
        if raw is None:
            return [], watermark

        try:
            events = json.loads(raw)
        except json.JSONDecodeError:
            return [], watermark

        if not isinstance(events, list):
            return [], watermark

        max_events = config.get("max_events", 20)
        events = events[:max_events]

        is_bootstrap = not self._bootstrapped
        items = []
        now = self._now_utc()

        new_event_snapshot = {}

        for event in events:
            event_id = event.get("id", "")
            if not event_id:
                continue

            summary = event.get("summary", "")[:100]
            start_dt = event.get("start", {}).get("dateTime", "")
            end_dt = event.get("end", {}).get("dateTime", "")
            updated = event.get("updated", "")
            html_link = event.get("htmlLink", "")
            organizer = event.get("organizer", {})
            organizer_email = organizer.get("email", "")
            organizer_name = organizer.get("displayName", "")

            # Build a serialisable snapshot value for change detection
            event_value = f"{summary}|{start_dt}|{end_dt}|{updated}"
            new_event_snapshot[event_id] = event_value

            if not is_bootstrap:
                # --- Event changed detection ---
                prev_value = self._event_snapshot.get(event_id)
                if prev_value is not None and prev_value != event_value:
                    items.append({
                        "id": f"calendar-changed-{event_id}",
                        "source": "calendar",
                        "type": "event_changed",
                        "title": f"Event updated: {summary}",
                        "preview": f"Calendar event '{summary}' was modified",
                        "discovered_at": self._utc_now_z(),
                        "author": organizer_email,
                        "author_name": organizer_name,
                        "group": "Calendar",
                        "url": html_link,
                        "metadata": {
                            "event_id": event_id,
                            "start": start_dt,
                            "end": end_dt,
                            "updated": updated,
                        },
                    })

                # --- Meeting reminders ---
                if start_dt:
                    try:
                        start_time = datetime.fromisoformat(start_dt)
                        # Ensure timezone-aware comparison
                        if start_time.tzinfo is None:
                            start_time = start_time.replace(tzinfo=timezone.utc)
                        minutes_until = (start_time - now).total_seconds() / 60

                        reminder_minutes = config.get("reminder_minutes", [30, 10])
                        for mins in sorted(reminder_minutes, reverse=True):
                            if 0 <= minutes_until <= mins:
                                remind_key = f"{event_id}-{mins}"
                                if self._reminded_snapshot.get(remind_key):
                                    continue
                                self._reminded_snapshot[remind_key] = self._utc_now_z()
                                items.append({
                                    "id": f"calendar-reminder-{event_id}-{mins}",
                                    "source": "calendar",
                                    "type": "meeting_reminder",
                                    "title": f"Meeting in ~{mins}min: {summary}",
                                    "preview": f"'{summary}' starts in ~{int(minutes_until)} minutes",
                                    "discovered_at": self._utc_now_z(),
                                    "author": organizer_email,
                                    "author_name": organizer_name,
                                    "group": "Calendar",
                                    "url": html_link,
                                    "metadata": {
                                        "event_id": event_id,
                                        "start": start_dt,
                                        "end": end_dt,
                                        "reminder_minutes": mins,
                                        "minutes_until": round(minutes_until, 1),
                                    },
                                })
                    except (ValueError, TypeError):
                        pass

        # Update snapshots
        self._event_snapshot = new_event_snapshot
        save_snapshot("calendar_events", self._event_snapshot)
        save_snapshot("calendar_reminded", self._reminded_snapshot)
        self._bootstrapped = True

        return items, self._utc_now_z()
