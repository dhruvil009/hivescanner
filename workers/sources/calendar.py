"""Calendar scanner — surfaces meeting reminders via `gws` CLI (Google Workspace)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone


def _reject_duplicate_key(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _strict_json(raw: bytes | str) -> object:
    return json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_key,
        parse_constant=_reject_constant,
    )

# Resolve imports whether run as module or standalone
try:
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists


class CalendarScanner:
    name = "calendar"
    _POLL_BUDGET_SECONDS = 45

    def __init__(self):
        self._cli_available = None
        self._event_snapshot = load_snapshot("calendar_events")
        self._reminded_snapshot = load_snapshot("calendar_reminded")
        self._bootstrapped = snapshot_exists("calendar_events")
        self._reminded_bootstrapped = snapshot_exists("calendar_reminded")

    def configure(self) -> dict:
        return {
            "enabled": False,
            "reminder_minutes": [30, 10],
            "max_events": 1000,
            "max_pages": 10,
            "lookahead_days": 30,
            "calendars": [],
            "timezone": "",
            "filter_declined": True,
            "noise_subjects": ["Focus Time", "Lunch", "OOO"],
        }

    def _gws(self, args: list[str], timeout: int = 15) -> str | None:
        """Run gws CLI command, return stdout or None on failure."""
        deadline = getattr(self, "_poll_deadline", None)
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("[calendar] poll time budget exhausted", file=sys.stderr)
                return None
            timeout = min(timeout, max(0.1, remaining))
        try:
            environment = os.environ.copy()
            for name in (
                "GWS_SANITIZE_TEMPLATE",
                "GOOGLE_WORKSPACE_CLI_SANITIZE_TEMPLATE",
                "GOOGLE_WORKSPACE_CLI_SANITIZE_MODE",
                "GOOGLE_WORKSPACE_CLI_LOG",
                "GOOGLE_WORKSPACE_CLI_LOG_FILE",
            ):
                environment.pop(name, None)
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                result = subprocess.run(
                    ["gws"] + args,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout,
                    env=environment,
                )
                stdout_file.seek(0)
                raw_stdout = stdout_file.read(5_000_001)
                stderr_file.seek(0)
                raw_stderr = stderr_file.read(201)
            if result.returncode != 0:
                print(
                    f"[calendar] gws error: {raw_stderr.decode('utf-8', errors='replace')}",
                    file=sys.stderr,
                )
                return None
            if len(raw_stdout) > 5_000_000:
                print("[calendar] gws output exceeded 5 MB", file=sys.stderr)
                return None
            return raw_stdout.decode("utf-8")
        except (subprocess.TimeoutExpired, OSError, UnicodeError, ValueError) as e:
            print(f"[calendar] gws failed: {e}", file=sys.stderr)
            return None

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _valid_cli_value(value: object, *, max_length: int) -> bool:
        return (
            isinstance(value, str)
            and 0 < len(value) <= max_length
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )

    @staticmethod
    def _rfc3339(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            return ""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
        return value if parsed.tzinfo is not None else ""

    @classmethod
    def _candidate_committed(cls, watermark: str, candidate_watermark: str) -> bool:
        current = cls._rfc3339(watermark)
        candidate = cls._rfc3339(candidate_watermark)
        if not current or not candidate:
            return False
        return datetime.fromisoformat(current.replace("Z", "+00:00")) >= datetime.fromisoformat(
            candidate.replace("Z", "+00:00")
        )

    @classmethod
    def _valid_event_state(cls, value: object) -> dict[str, dict] | None:
        if not isinstance(value, dict) or len(value) > 5_000:
            return None
        clean: dict[str, dict] = {}
        expected_keys = {
            "calendar_id",
            "summary",
            "start",
            "end",
            "updated",
            "status",
            "ignored",
        }
        for key, event in value.items():
            if not isinstance(key, str) or key.count("\0") != 1:
                return None
            calendar_id, event_id = key.split("\0", 1)
            if (
                not cls._valid_cli_value(calendar_id, max_length=256)
                or not cls._valid_cli_value(event_id, max_length=256)
                or not isinstance(event, dict)
                or set(event) != expected_keys
                or event.get("calendar_id") != calendar_id
                or not isinstance(event.get("summary"), str)
                or len(event["summary"]) > 100
                or not isinstance(event.get("start"), str)
                or (event["start"] and not cls._rfc3339(event["start"]))
                or not isinstance(event.get("end"), str)
                or (event["end"] and not cls._rfc3339(event["end"]))
                or not cls._rfc3339(event.get("updated"))
                or event.get("status") not in {"confirmed", "tentative", "cancelled"}
                or not isinstance(event.get("ignored"), bool)
            ):
                return None
            clean[key] = dict(event)
        return clean

    @classmethod
    def _valid_reminder_state(cls, value: object) -> dict[str, str] | None:
        if not isinstance(value, dict) or len(value) > 5_000:
            return None
        clean: dict[str, str] = {}
        for key, observed_at in value.items():
            if (
                not isinstance(key, str)
                or re.fullmatch(r"[0-9a-f]{16}\|[0-9a-f]{12}\|[1-9]\d{0,4}", key)
                is None
                or not cls._rfc3339(observed_at)
            ):
                return None
            clean[key] = observed_at
        return clean

    @classmethod
    def _prepare_event_state(
        cls, snapshot: dict, watermark: str, initialized: bool
    ) -> tuple[dict[str, dict], bool] | None:
        if snapshot.get("schema_version") == 2:
            if (
                type(snapshot.get("schema_version")) is not int
                or set(snapshot)
                != {
                    "schema_version",
                    "committed",
                    "candidate",
                    "candidate_watermark",
                    "bootstrap_pending",
                }
                or not isinstance(snapshot.get("bootstrap_pending"), bool)
                or not cls._rfc3339(snapshot.get("candidate_watermark"))
            ):
                return None
            committed = cls._valid_event_state(snapshot.get("committed"))
            candidate = cls._valid_event_state(snapshot.get("candidate"))
            if committed is None or candidate is None:
                return None
            candidate_wm = snapshot["candidate_watermark"]
            bootstrap = snapshot["bootstrap_pending"]
            if cls._candidate_committed(watermark, candidate_wm):
                committed = candidate
                bootstrap = False
            return committed, bootstrap
        if "schema_version" in snapshot:
            return None
        if (
            not isinstance(snapshot, dict)
            or len(snapshot) > 5_000
            or not all(
                isinstance(key, str)
                and 1 <= len(key) <= 256
                and isinstance(value, str)
                and len(value) <= 50_000
                for key, value in snapshot.items()
            )
            or (not initialized and snapshot)
        ):
            return None
        # Legacy values concatenated fields ambiguously. A one-time quiet
        # bootstrap avoids reporting every migrated event as changed.
        return {}, initialized

    @classmethod
    def _prepare_reminder_state(
        cls, snapshot: dict, watermark: str, initialized: bool
    ) -> dict[str, str] | None:
        if snapshot.get("schema_version") == 2:
            if (
                type(snapshot.get("schema_version")) is not int
                or set(snapshot)
                != {
                    "schema_version",
                    "committed",
                    "candidate",
                    "candidate_watermark",
                }
                or not cls._rfc3339(snapshot.get("candidate_watermark"))
            ):
                return None
            committed = cls._valid_reminder_state(snapshot.get("committed"))
            candidate = cls._valid_reminder_state(snapshot.get("candidate"))
            if committed is None or candidate is None:
                return None
            if cls._candidate_committed(watermark, snapshot["candidate_watermark"]):
                committed = candidate
            return committed
        if "schema_version" in snapshot:
            return None
        if (
            not isinstance(snapshot, dict)
            or len(snapshot) > 5_000
            or not all(
                isinstance(key, str)
                and 1 <= len(key) <= 512
                and isinstance(value, str)
                and 1 <= len(value) <= 64
                for key, value in snapshot.items()
            )
            or (not initialized and snapshot)
        ):
            return None
        return {}

    @classmethod
    def _normalize_event(cls, event: object) -> dict | None:
        if not isinstance(event, dict) or not cls._valid_cli_value(
            event.get("id"), max_length=256
        ):
            return None
        status = event.get("status", "confirmed")
        summary = event.get("summary", "")
        updated = event.get("updated")
        html_link = event.get("htmlLink", "")
        if (
            status not in {"confirmed", "tentative", "cancelled"}
            or not isinstance(summary, str)
            or len(summary) > 10_000
            or not cls._rfc3339(updated)
            or not isinstance(html_link, str)
            or len(html_link) > 2048
            or any(
                ord(char) < 32 or ord(char) == 127
                for char in html_link
            )
        ):
            return None

        normalized_bounds = {}
        for field in ("start", "end"):
            raw = event.get(field)
            if raw is None and status == "cancelled":
                normalized_bounds[field] = {}
                continue
            if not isinstance(raw, dict):
                return None
            date_time = raw.get("dateTime", "")
            date = raw.get("date", "")
            if status == "cancelled" and not date_time and not date:
                normalized_bounds[field] = {}
                continue
            if bool(date_time) == bool(date):
                return None
            if date_time:
                if not cls._rfc3339(date_time):
                    return None
                normalized_bounds[field] = {"dateTime": date_time}
            else:
                if not isinstance(date, str):
                    return None
                try:
                    parsed_date = datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    return None
                if parsed_date.strftime("%Y-%m-%d") != date:
                    return None
                normalized_bounds[field] = {"date": date}

        organizer = event.get("organizer", {})
        if not isinstance(organizer, dict):
            return None
        organizer_email = organizer.get("email", "")
        organizer_name = organizer.get("displayName", "")
        if (
            not isinstance(organizer_email, str)
            or len(organizer_email) > 320
            or not isinstance(organizer_name, str)
            or len(organizer_name) > 500
            or any(
                ord(char) < 32 or ord(char) == 127
                for char in organizer_email
            )
        ):
            return None

        attendees = event.get("attendees", [])
        if not isinstance(attendees, list) or len(attendees) > 10_000:
            return None
        clean_attendees = []
        for attendee in attendees:
            if not isinstance(attendee, dict):
                return None
            is_self = attendee.get("self", False)
            response_status = attendee.get("responseStatus", "needsAction")
            if type(is_self) is not bool or response_status not in {
                "needsAction",
                "declined",
                "tentative",
                "accepted",
            }:
                return None
            clean_attendees.append(
                {"self": is_self, "responseStatus": response_status}
            )

        return {
            "id": event["id"],
            "summary": summary,
            "start": normalized_bounds["start"],
            "end": normalized_bounds["end"],
            "updated": updated,
            "status": status,
            "htmlLink": html_link,
            "organizer": {
                "email": organizer_email,
                "displayName": organizer_name,
            },
            "attendees": clean_attendees,
        }

    def _gws_collection_page(
        self, args: list[str], *, collection_name: str
    ) -> tuple[list[object], str | None] | None:
        raw = self._gws(args)
        if raw is None:
            return None
        try:
            data = _strict_json(raw)
        except (json.JSONDecodeError, ValueError):
            print(f"[calendar] invalid {collection_name} JSON", file=sys.stderr)
            return None
        if (
            not isinstance(data, dict)
            or "error" in data
            or ("events" in data and "items" not in data)
            or not isinstance(data.get("items", []), list)
        ):
            print(f"[calendar] malformed {collection_name} response", file=sys.stderr)
            return None
        token = data.get("nextPageToken")
        if token in (None, ""):
            token = None
        elif not self._valid_cli_value(token, max_length=4096):
            print(f"[calendar] malformed {collection_name} page token", file=sys.stderr)
            return None
        return data.get("items", []), token

    def _list_calendar_ids(self, max_pages: int) -> list[str] | None:
        calendar_ids: list[str] = []
        seen_ids: set[str] = set()
        seen_page_tokens: set[str] = set()
        page_token: str | None = None
        for _ in range(max_pages):
            params: dict[str, object] = {"maxResults": 250}
            if page_token:
                params["pageToken"] = page_token
            page = self._gws_collection_page(
                [
                    "calendar",
                    "calendarList",
                    "list",
                    "--params",
                    json.dumps(params, separators=(",", ":")),
                    "--format",
                    "json",
                ],
                collection_name="calendar list",
            )
            if page is None:
                return None
            entries, page_token = page
            if len(entries) > 250:
                print("[calendar] oversized calendar list page", file=sys.stderr)
                return None
            for entry in entries:
                if not isinstance(entry, dict) or not self._valid_cli_value(
                    entry.get("id"), max_length=256
                ):
                    print("[calendar] malformed calendar list entry", file=sys.stderr)
                    return None
                calendar_id = entry["id"]
                if calendar_id not in seen_ids:
                    seen_ids.add(calendar_id)
                    calendar_ids.append(calendar_id)
                if len(calendar_ids) > 25:
                    print(
                        "[calendar] account has more than 25 calendars; configure "
                        "an explicit calendars list",
                        file=sys.stderr,
                    )
                    return None
            if not page_token:
                return calendar_ids
            if page_token in seen_page_tokens:
                print("[calendar] calendar list repeated a page token", file=sys.stderr)
                return None
            seen_page_tokens.add(page_token)
        print("[calendar] calendar list exceeded max_pages", file=sys.stderr)
        return None

    def _fetch_events(self, config: dict, max_events: int) -> list[dict] | None:
        calendars = config.get("calendars", [])
        if not isinstance(calendars, list):
            print("[calendar] calendars must be a list", file=sys.stderr)
            return None
        if len(calendars) > 25:
            print("[calendar] at most 25 calendars may be configured", file=sys.stderr)
            return None
        lookahead_days = config.get("lookahead_days", 30)
        max_pages = config.get("max_pages", 10)
        if (
            isinstance(lookahead_days, bool)
            or not isinstance(lookahead_days, int)
            or not 1 <= lookahead_days <= 90
            or isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= 100
        ):
            print("[calendar] paging window is invalid", file=sys.stderr)
            return None

        timezone_value = config.get("timezone") or ""
        if not isinstance(timezone_value, str):
            print("[calendar] timezone must be a string", file=sys.stderr)
            return None
        if timezone_value != timezone_value.strip():
            print("[calendar] timezone must not have surrounding whitespace", file=sys.stderr)
            return None
        timezone_name = timezone_value
        if timezone_name and not self._valid_cli_value(timezone_name, max_length=100):
            print("[calendar] timezone is invalid or exceeds 100 characters", file=sys.stderr)
            return None

        targets: list[str] = []
        seen_targets: set[str] = set()
        for calendar_id in calendars:
            if not self._valid_cli_value(calendar_id, max_length=256):
                print("[calendar] calendar IDs must be bounded strings", file=sys.stderr)
                return None
            if calendar_id in seen_targets:
                print("[calendar] calendar IDs must be unique", file=sys.stderr)
                return None
            seen_targets.add(calendar_id)
            targets.append(calendar_id)
        if not targets:
            listed_targets = self._list_calendar_ids(max_pages)
            if listed_targets is None:
                return None
            targets = listed_targets

        now = self._now_utc()
        time_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = (now + timedelta(days=lookahead_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        events: list[dict] = []
        seen_events: dict[str, dict] = {}
        for calendar_id in targets:
            page_token: str | None = None
            seen_page_tokens: set[str] = set()
            for _ in range(max_pages):
                params: dict[str, object] = {
                    "calendarId": calendar_id,
                    "maxResults": min(2500, max_events + 1),
                    "orderBy": "startTime",
                    "showDeleted": True,
                    "singleEvents": True,
                    "timeMax": time_max,
                    "timeMin": time_min,
                }
                if timezone_name:
                    params["timeZone"] = timezone_name
                if page_token:
                    params["pageToken"] = page_token
                page = self._gws_collection_page(
                    [
                        "calendar",
                        "events",
                        "list",
                        "--params",
                        json.dumps(params, separators=(",", ":")),
                        "--format",
                        "json",
                    ],
                    collection_name="event list",
                )
                if page is None:
                    return None
                page_events, page_token = page
                if len(page_events) > min(2500, max_events + 1):
                    print("[calendar] oversized event list page", file=sys.stderr)
                    return None
                for event in page_events:
                    normalized_event = self._normalize_event(event)
                    if normalized_event is None:
                        print("[calendar] malformed event list entry", file=sys.stderr)
                        return None
                    event_id = normalized_event["id"]
                    dedup_key = f"{calendar_id}\0{event_id}"
                    previous = seen_events.get(dedup_key)
                    if previous is not None:
                        if previous != normalized_event:
                            print(
                                "[calendar] duplicate event changed within one poll",
                                file=sys.stderr,
                            )
                            return None
                        continue
                    seen_events[dedup_key] = normalized_event
                    event_copy = dict(normalized_event)
                    event_copy["_calendar_id"] = calendar_id
                    events.append(event_copy)
                    if len(events) > max_events:
                        return events
                if not page_token:
                    break
                if page_token in seen_page_tokens:
                    print("[calendar] event list repeated a page token", file=sys.stderr)
                    return None
                seen_page_tokens.add(page_token)
            else:
                print("[calendar] event list exceeded max_pages", file=sys.stderr)
                return None
        return events

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        if not isinstance(config, dict) or not isinstance(watermark, str):
            return [], watermark
        if watermark and not self._rfc3339(watermark):
            print("[calendar] invalid watermark; preserving it", file=sys.stderr)
            return [], watermark

        filter_declined = config.get("filter_declined", True)
        if type(filter_declined) is not bool:
            print("[calendar] filter_declined must be a boolean", file=sys.stderr)
            return [], watermark

        reminder_minutes = config.get("reminder_minutes")
        if reminder_minutes is None:
            reminder_minutes = [
                config.get("prep_minutes_before", 30),
                config.get("reminder_minutes_before", 10),
            ]
        if (
            not isinstance(reminder_minutes, list)
            or len(reminder_minutes) > 100
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 10080
                for value in reminder_minutes
            )
        ):
            print("[calendar] reminder_minutes must be bounded positive integers", file=sys.stderr)
            return [], watermark
        if len(set(reminder_minutes)) != len(reminder_minutes):
            print("[calendar] reminder_minutes entries must be unique", file=sys.stderr)
            return [], watermark
        reminder_thresholds = sorted(set(reminder_minutes))

        max_events = config.get("max_events", 1000)
        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or not 1 <= max_events <= 5000
        ):
            print("[calendar] max_events must be an integer from 1 to 5000", file=sys.stderr)
            return [], watermark

        calendars = config.get("calendars", [])
        lookahead_days = config.get("lookahead_days", 30)
        max_pages = config.get("max_pages", 10)
        timezone_value = config.get("timezone", "")
        if (
            not isinstance(calendars, list)
            or len(calendars) > 25
            or not all(
                self._valid_cli_value(value, max_length=256) for value in calendars
            )
            or len(set(calendars)) != len(calendars)
            or isinstance(lookahead_days, bool)
            or not isinstance(lookahead_days, int)
            or not 1 <= lookahead_days <= 90
            or isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= 100
            or not isinstance(timezone_value, str)
            or timezone_value != timezone_value.strip()
            or (
                timezone_value
                and not self._valid_cli_value(timezone_value, max_length=100)
            )
        ):
            print("[calendar] calendar paging configuration is invalid", file=sys.stderr)
            return [], watermark

        configured_noise = config.get("noise_subjects", [])
        if (
            not isinstance(configured_noise, list)
            or len(configured_noise) > 500
            or not all(
                isinstance(value, str) and len(value) <= 200
                for value in configured_noise
            )
        ):
            print("[calendar] noise_subjects must contain bounded strings", file=sys.stderr)
            return [], watermark
        normalized_noise = [value.strip().casefold() for value in configured_noise]
        if len(set(normalized_noise)) != len(normalized_noise):
            print("[calendar] noise_subjects entries must be unique", file=sys.stderr)
            return [], watermark
        noise_subjects = set(normalized_noise)

        prepared_events = self._prepare_event_state(
            self._event_snapshot, watermark, self._bootstrapped
        )
        committed_reminders = self._prepare_reminder_state(
            self._reminded_snapshot, watermark, self._reminded_bootstrapped
        )
        if prepared_events is None or committed_reminders is None:
            print("[calendar] invalid persisted snapshot; preserving watermark", file=sys.stderr)
            return [], watermark
        committed_events, bootstrap_pending = prepared_events

        if self._cli_available is None:
            self._cli_available = ensure_tool("gws")
        if not self._cli_available:
            return [], watermark

        scan_started_at = self._utc_now_z()
        events = self._fetch_events(config, max_events)
        if events is None:
            return [], watermark
        if len(events) > max_events:
            print(
                f"[calendar] event list exceeded max_events ({max_events}); preserving state",
                file=sys.stderr,
            )
            return [], watermark

        is_bootstrap = not self._bootstrapped or bootstrap_pending
        items = []
        now = self._now_utc()

        new_event_snapshot = {}
        new_reminded_snapshot = dict(committed_reminders)
        for event in events:
            event_id = str(event.get("id") or "")
            calendar_id = str(event.get("_calendar_id") or "primary")
            if not event_id or len(event_id) > 256:
                continue
            event_key = f"{calendar_id}\0{event_id}"
            event_hash = hashlib.sha256(event_key.encode()).hexdigest()[:16]

            summary = (event.get("summary") or "(untitled event)")[:100]
            start_obj = event.get("start") if isinstance(event.get("start"), dict) else {}
            end_obj = event.get("end") if isinstance(event.get("end"), dict) else {}
            start_dt = str(start_obj.get("dateTime") or "")
            end_dt = str(end_obj.get("dateTime") or "")
            updated = str(event.get("updated") or "")
            event_status = str(event.get("status") or "confirmed")
            html_link = str(event.get("htmlLink") or "")
            organizer = event.get("organizer") if isinstance(event.get("organizer"), dict) else {}
            organizer_email = str(organizer.get("email") or "")
            organizer_name = str(organizer.get("displayName") or "")

            attendees = event.get("attendees") if isinstance(event.get("attendees"), list) else []
            declined = any(
                attendee.get("self") is True and attendee.get("responseStatus") == "declined"
                for attendee in attendees if isinstance(attendee, dict)
            )
            all_day = bool(start_obj.get("date") and not start_dt)
            ignored = all_day or summary.strip().casefold() in noise_subjects or (
                filter_declined and declined
            )

            # Build a serialisable snapshot value for change detection
            event_value = {
                "calendar_id": calendar_id,
                "summary": summary,
                "start": start_dt,
                "end": end_dt,
                "updated": updated,
                "status": event_status,
                "ignored": ignored,
            }
            new_event_snapshot[event_key] = event_value

            if ignored:
                continue

            if not is_bootstrap:
                # --- Event changed detection ---
                prev_value = committed_events.get(event_key)
                if prev_value is None:
                    # One-time compatibility with snapshots keyed only by the
                    # event ID before multi-calendar collision handling.
                    prev_value = committed_events.get(event_id)
                recent_new_event = True
                if prev_value is None and watermark and not watermark.startswith("1970-"):
                    recent_new_event = False
                    if updated:
                        try:
                            recent_new_event = datetime.fromisoformat(
                                updated.replace("Z", "+00:00")
                            ) >= (
                                datetime.fromisoformat(watermark.replace("Z", "+00:00"))
                                - timedelta(minutes=5)
                            )
                        except (TypeError, ValueError):
                            pass
                if (
                    not event_value.get("ignored")
                    and (
                        (prev_value is None and recent_new_event)
                        or (
                            isinstance(prev_value, dict)
                            and (prev_value.get("ignored") or prev_value != event_value)
                        )
                    )
                ):
                    change_hash = hashlib.sha256(
                        json.dumps(event_value, sort_keys=True).encode()
                    ).hexdigest()[:10]
                    is_new = prev_value is None
                    is_cancelled = event_status == "cancelled"
                    items.append({
                        "id": f"calendar-changed-{event_hash}-{change_hash}",
                        "source": "calendar",
                        "type": "event_changed",
                        "title": (
                            f"Event cancelled: {summary}"
                            if is_cancelled
                            else f"New event: {summary}"
                            if is_new
                            else f"Event updated: {summary}"
                        ),
                        "preview": (
                            f"Calendar event '{summary}' was cancelled"
                            if is_cancelled
                            else f"Calendar event '{summary}' was added"
                            if is_new
                            else f"Calendar event '{summary}' was modified"
                        ),
                        "discovered_at": self._utc_now_z(),
                        "author": organizer_email,
                        "author_name": organizer_name,
                        "group": "Calendar",
                        "url": html_link,
                        "metadata": {
                            "event_id": event_id,
                            "calendar_id": calendar_id,
                            "start": start_dt,
                            "end": end_dt,
                            "updated": updated,
                        },
                    })

                if event_status == "cancelled":
                    continue

                # --- Meeting reminders ---
                if start_dt:
                    try:
                        start_time = datetime.fromisoformat(start_dt.replace("Z", "+00:00"))
                        # Ensure timezone-aware comparison
                        if start_time.tzinfo is None:
                            start_time = start_time.replace(tzinfo=timezone.utc)
                        minutes_until = (start_time - now).total_seconds() / 60

                        start_hash = hashlib.sha256(start_dt.encode()).hexdigest()[:12]
                        for mins in reminder_thresholds:
                            if 0 <= minutes_until <= mins:
                                remind_key = f"{event_hash}|{start_hash}|{mins}"
                                if committed_reminders.get(remind_key):
                                    continue
                                new_reminded_snapshot[remind_key] = scan_started_at
                                items.append({
                                    "id": f"calendar-reminder-{event_hash}-{start_hash}-{mins}",
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
                                        "calendar_id": calendar_id,
                                        "start": start_dt,
                                        "end": end_dt,
                                        "reminder_minutes": mins,
                                        "minutes_until": round(minutes_until, 1),
                                    },
                                })
                                break
                    except (ValueError, TypeError, OverflowError):
                        pass

        # Update snapshots
        self._event_snapshot = {
            "schema_version": 2,
            "committed": committed_events,
            "candidate": new_event_snapshot,
            "candidate_watermark": scan_started_at,
            "bootstrap_pending": is_bootstrap,
        }
        active_reminder_prefixes = {
            f"{hashlib.sha256(event_key.encode()).hexdigest()[:16]}|"
            f"{hashlib.sha256(str(value.get('start') or '').encode()).hexdigest()[:12]}"
            for event_key, value in new_event_snapshot.items()
            if isinstance(value, dict) and value.get("start")
        }
        new_reminded_snapshot = {
            key: value for key, value in new_reminded_snapshot.items()
            if key.rsplit("|", 1)[0] in active_reminder_prefixes
        }
        self._reminded_snapshot = {
            "schema_version": 2,
            "committed": committed_reminders,
            "candidate": new_reminded_snapshot,
            "candidate_watermark": scan_started_at,
        }
        save_snapshot("calendar_events", self._event_snapshot)
        save_snapshot("calendar_reminded", self._reminded_snapshot)
        self._bootstrapped = True
        self._reminded_bootstrapped = True

        return items, scan_started_at
