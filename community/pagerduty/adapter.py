"""PagerDuty scanner that tracks the live incident set and status transitions."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        try:
            source_port = source.port or (443 if source.scheme == "https" else 80)
            target_port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            source_port, target_port = -2, -1
        if (
            source.scheme.casefold() != target.scheme.casefold()
            or (source.hostname or "").casefold() != (target.hostname or "").casefold()
            or source_port != target_port
        ):
            raise urllib.error.HTTPError(
                newurl, code, "cross-origin redirect refused", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_SameOriginRedirectHandler()).open(
        req, timeout=timeout
    )


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


def _bounded_text(value: object, maximum: int, *, allow_empty: bool = True) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= maximum
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _normalize_active_incident(value: object) -> tuple[str, dict] | None:
    if not isinstance(value, dict):
        return None
    incident_id = value.get("id")
    status = value.get("status")
    title = value.get("title")
    urgency = value.get("urgency")
    service = value.get("service")
    assignments = value.get("assignments")
    html_url = value.get("html_url")
    incident_number = value.get("incident_number")
    changed_at = value.get("last_status_change_at")
    if (
        not _bounded_text(incident_id, 128, allow_empty=False)
        or not isinstance(status, str)
        or status not in {"triggered", "acknowledged"}
        or not _bounded_text(title, 10_000)
        or not isinstance(urgency, str)
        or urgency not in {"high", "low"}
        or not isinstance(service, dict)
        or not _bounded_text(service.get("id"), 128, allow_empty=False)
        or not _bounded_text(service.get("summary", ""), 10_000)
        or not isinstance(assignments, list)
        or len(assignments) > 1_000
        or not _bounded_text(html_url, 2_000)
        or isinstance(incident_number, bool)
        or not isinstance(incident_number, int)
        or not 0 <= incident_number <= 10**15
        or not _bounded_text(changed_at, 128, allow_empty=False)
    ):
        return None
    normalized_assignments: list[dict] = []
    for assignment in assignments:
        assignee = assignment.get("assignee") if isinstance(assignment, dict) else None
        if (
            not isinstance(assignee, dict)
            or not _bounded_text(assignee.get("id"), 128, allow_empty=False)
            or not _bounded_text(assignee.get("summary", ""), 10_000)
        ):
            return None
        normalized_assignments.append(
            {"id": assignee["id"], "summary": assignee.get("summary", "")}
        )
    first_assignee = normalized_assignments[0] if normalized_assignments else {}
    return incident_id, {
        "status": status,
        "title": title[:200],
        "urgency": urgency,
        "service_name": service.get("summary", "")[:200],
        "html_url": html_url[:1000],
        "incident_number": incident_number,
        "assignee_id": first_assignee.get("id", ""),
        "assignee_name": first_assignee.get("summary", "")[:200],
        "status_changed_at": changed_at,
    }


def _valid_stored_incident(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("status"), str)
        and value["status"] in {"triggered", "acknowledged"}
        and _bounded_text(value.get("title"), 200)
        and isinstance(value.get("urgency"), str)
        and value["urgency"] in {"high", "low"}
        and _bounded_text(value.get("service_name"), 200)
        and _bounded_text(value.get("html_url"), 1_000)
        and isinstance(value.get("incident_number"), int)
        and not isinstance(value.get("incident_number"), bool)
        and 0 <= value["incident_number"] <= 10**15
        and _bounded_text(value.get("assignee_id"), 128)
        and _bounded_text(value.get("assignee_name"), 200)
        and _bounded_text(
            value.get("status_changed_at"), 128, allow_empty=False
        )
    )


class PagerDutyScanner:
    name = "pagerduty"
    MAX_TRACKED_INCIDENTS = 100
    MAX_DETAIL_CHECKS_PER_POLL = 10
    _POLL_BUDGET_SECONDS = 45

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "PAGERDUTY_TOKEN",
            "user_id": "",
            "team_ids": [],
            "service_ids": [],
            "max_items": 100,
            "max_pages": 10,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(self, path: str, token: str, params: Optional[dict] = None) -> Optional[dict]:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"https://api.pagerduty.com{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Token token={token}",
                "Accept": "application/vnd.pagerduty+json;version=2",
            },
        )
        deadline = getattr(self, "_poll_deadline", None)
        timeout = 15.0
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            timeout = min(timeout, max(0.1, remaining))
        try:
            with _urlopen(req, timeout=timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("response exceeded 2 MB")
                result = _strict_json(raw)
                return result if isinstance(result, dict) else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"_not_found": True}
            retry_after = exc.headers.get("Retry-After", "")
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[pagerduty] HTTP {exc.code}{suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[pagerduty] API error: {exc}", file=sys.stderr)
        return None

    @staticmethod
    def _load_state(watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") in {2, 3}:
            incidents = state.get("incidents")
            if (
                not isinstance(state.get("initialized"), bool)
                or not isinstance(incidents, dict)
                or len(incidents) > PagerDutyScanner.MAX_TRACKED_INCIDENTS
                or not all(
                    _bounded_text(incident_id, 128, allow_empty=False)
                    and _valid_stored_incident(value)
                    for incident_id, value in incidents.items()
                )
                or (
                    "scope" in state
                    and not (
                        isinstance(state["scope"], str)
                        and re.fullmatch(r"[0-9a-f]{16}", state["scope"])
                    )
                )
                or not _bounded_text(state.get("detail_cursor", ""), 128)
            ):
                return None
            return state
        if isinstance(watermark, str) and watermark.lstrip().startswith(("{", "[")):
            return None
        legacy = watermark if isinstance(watermark, str) else ""
        return {
            "version": 3,
            "initialized": bool(legacy and not legacy.startswith("1970-")),
            "incidents": {},
        }

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        token_env = config.get("token_env", "PAGERDUTY_TOKEN")
        if not isinstance(token_env, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", token_env
        ):
            return [], watermark
        token = os.environ.get(token_env, "")
        if (
            not token
            or len(token) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in token)
        ):
            return [], watermark
        page_size = config.get("max_items", 100)
        max_pages = config.get("max_pages", 10)
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
            or isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= 10
        ):
            print("[pagerduty] pagination limits are invalid", file=sys.stderr)
            return [], watermark
        user_id = config.get("user_id", "")
        raw_team_ids = config.get("team_ids", [])
        raw_service_ids = config.get("service_ids", [])
        if (
            not isinstance(user_id, str)
            or not isinstance(raw_team_ids, list)
            or not isinstance(raw_service_ids, list)
            or not all(
                isinstance(value, str)
                for value in [*raw_team_ids, *raw_service_ids]
            )
        ):
            return [], watermark
        team_ids = list(dict.fromkeys(raw_team_ids))
        service_ids = list(dict.fromkeys(raw_service_ids))
        if (
            len(team_ids) > 100
            or len(service_ids) > 100
            or (user_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", user_id))
            or any(
                re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None
                for value in [*team_ids, *service_ids]
            )
        ):
            print("[pagerduty] at most 100 team and service IDs may be configured", file=sys.stderr)
            return [], watermark
        scope = hashlib.sha256(
            json.dumps(
                {
                    "user_id": user_id,
                    "team_ids": sorted(team_ids),
                    "service_ids": sorted(service_ids),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]
        base_params: dict[str, object] = {
            "statuses[]": ["triggered", "acknowledged"],
            "date_range": "all",
            "limit": page_size,
            "sort_by": "created_at:asc",
        }
        if user_id:
            base_params["user_ids[]"] = [user_id]
        if team_ids:
            base_params["team_ids[]"] = team_ids
        if service_ids:
            base_params["service_ids[]"] = service_ids

        incidents: list[dict] = []
        offset = 0
        for page in range(max_pages):
            result = self._api("/incidents", token, {**base_params, "offset": offset})
            if (
                result is None
                or not isinstance(result.get("incidents"), list)
                or not isinstance(result.get("more"), bool)
            ):
                return [], watermark
            page_incidents = result["incidents"]
            if not all(isinstance(value, dict) for value in page_incidents):
                print("[pagerduty] malformed incident list", file=sys.stderr)
                return [], watermark
            incidents.extend(page_incidents)
            if len(incidents) > self.MAX_TRACKED_INCIDENTS:
                print(
                    f"[pagerduty] more than {self.MAX_TRACKED_INCIDENTS} active incidents; use webhooks",
                    file=sys.stderr,
                )
                return [], watermark
            if not result["more"]:
                break
            offset += len(page_incidents)
            if not page_incidents or page + 1 >= max_pages:
                print("[pagerduty] active incidents exceeded max_pages", file=sys.stderr)
                return [], watermark

        state = self._load_state(watermark)
        if state is None:
            print("[pagerduty] invalid persisted state; preserving watermark", file=sys.stderr)
            return [], watermark
        same_scope = state.get("scope") == scope
        initialized = bool(state.get("initialized")) and same_scope
        previous_incidents = state.get("incidents", {}) if same_scope else {}
        current_incidents: dict[str, dict] = {}
        pollen: list[dict] = []
        discovered_at = self._utc_now_z()
        seen_incident_ids: set[str] = set()

        for incident in incidents:
            normalized = _normalize_active_incident(incident)
            if normalized is None or normalized[0] in seen_incident_ids:
                print("[pagerduty] malformed active incident", file=sys.stderr)
                return [], watermark
            incident_id, current = normalized
            seen_incident_ids.add(incident_id)
            current_incidents[incident_id] = current
            previous = previous_incidents.get(incident_id)
            if not initialized or (
                isinstance(previous, dict) and previous.get("status") == current["status"]
            ):
                continue
            pollen_type = (
                "pagerduty_triggered"
                if current["status"] == "triggered"
                else "pagerduty_acknowledged"
            )
            transition = hashlib.sha256(
                (
                    f"{previous.get('status') if isinstance(previous, dict) else 'new'}:"
                    f"{current['status']}:{current['status_changed_at']}"
                ).encode()
            ).hexdigest()[:10]
            pollen.append(
                self._incident_pollen(
                    incident_id, current, pollen_type, discovered_at, transition
                )
            )

        if initialized:
            missing = sorted([
                (incident_id, previous)
                for incident_id, previous in previous_incidents.items()
                if incident_id not in current_incidents and isinstance(previous, dict)
            ], key=lambda value: value[0])
            detail_cursor = state.get("detail_cursor", "")
            split_at = next(
                (
                    index for index, (incident_id, _) in enumerate(missing)
                    if incident_id > detail_cursor
                ),
                0,
            )
            ordered_missing = [*missing[split_at:], *missing[:split_at]]
            checked_missing = ordered_missing[: self.MAX_DETAIL_CHECKS_PER_POLL]
            for incident_id, previous in checked_missing:
                detail = self._api(
                    f"/incidents/{urllib.parse.quote(incident_id, safe='')}", token
                )
                raw_incident = detail.get("incident") if isinstance(detail, dict) else None
                if isinstance(detail, dict) and detail.get("_not_found") is True:
                    raw_incident = {"status": "deleted", "assignments": []}
                if not isinstance(raw_incident, dict):
                    # A transient detail failure must not turn a filter exit
                    # into a false resolved/unassigned notification.
                    current_incidents[incident_id] = previous
                    continue
                status = raw_incident.get("status")
                if not isinstance(status, str) or status not in {
                    "triggered",
                    "acknowledged",
                    "resolved",
                    "deleted",
                }:
                    current_incidents[incident_id] = previous
                    continue
                assignment_ids: set[str] = set()
                if user_id and status in {"triggered", "acknowledged"}:
                    assignments = raw_incident.get("assignments")
                    if not isinstance(assignments, list) or len(assignments) > 1_000:
                        current_incidents[incident_id] = previous
                        continue
                    malformed_assignments = False
                    for assignment in assignments:
                        assignee = (
                            assignment.get("assignee")
                            if isinstance(assignment, dict)
                            else None
                        )
                        if (
                            not isinstance(assignee, dict)
                            or not _bounded_text(
                                assignee.get("id"), 128, allow_empty=False
                            )
                        ):
                            malformed_assignments = True
                            break
                        assignment_ids.add(assignee["id"])
                    if malformed_assignments:
                        current_incidents[incident_id] = previous
                        continue
                incident_service_id = ""
                if service_ids and status in {"triggered", "acknowledged"}:
                    service = raw_incident.get("service")
                    if (
                        not isinstance(service, dict)
                        or not _bounded_text(
                            service.get("id"), 128, allow_empty=False
                        )
                    ):
                        current_incidents[incident_id] = previous
                        continue
                    incident_service_id = service["id"]
                incident_team_ids: set[str] = set()
                if team_ids and status in {"triggered", "acknowledged"}:
                    incident_teams = raw_incident.get("teams")
                    if not isinstance(incident_teams, list) or len(incident_teams) > 1_000:
                        current_incidents[incident_id] = previous
                        continue
                    malformed_teams = False
                    for team in incident_teams:
                        if (
                            not isinstance(team, dict)
                            or not _bounded_text(
                                team.get("id"), 128, allow_empty=False
                            )
                        ):
                            malformed_teams = True
                            break
                        incident_team_ids.add(team["id"])
                    if malformed_teams:
                        current_incidents[incident_id] = previous
                        continue
                if status == "resolved":
                    pollen_type = "pagerduty_resolved"
                    terminal_status = "resolved"
                elif status == "deleted":
                    pollen_type = "pagerduty_no_longer_matching"
                    terminal_status = "deleted"
                elif user_id and user_id not in assignment_ids:
                    pollen_type = "pagerduty_unassigned"
                    terminal_status = "unassigned"
                elif service_ids and incident_service_id not in service_ids:
                    pollen_type = "pagerduty_no_longer_matching"
                    terminal_status = "service-filtered"
                elif team_ids and incident_team_ids.isdisjoint(team_ids):
                    pollen_type = "pagerduty_no_longer_matching"
                    terminal_status = "team-filtered"
                elif status in {"triggered", "acknowledged"}:
                    # The list and detail endpoints can be briefly inconsistent.
                    # A detail record that is still active and still matches
                    # every provable filter is not a terminal transition.
                    current_incidents[incident_id] = previous
                    continue
                terminal = dict(previous)
                terminal["status"] = terminal_status
                raw_changed_at = raw_incident.get("last_status_change_at", "")
                terminal["status_changed_at"] = (
                    raw_changed_at
                    if _bounded_text(raw_changed_at, 128)
                    else previous.get("status_changed_at", "")
                )
                transition = hashlib.sha256(
                    (
                        f"{previous.get('status', '')}:{terminal['status']}:"
                        f"{terminal.get('status_changed_at', '')}"
                    ).encode()
                ).hexdigest()[:10]
                pollen.append(
                    self._incident_pollen(
                        incident_id, terminal, pollen_type, discovered_at, transition
                    )
                )
            # Bound external calls while retaining unchecked incidents for a
            # later poll. They remain live in state until individually proven
            # resolved, unassigned, deleted, or filtered.
            checked_ids = {incident_id for incident_id, _ in checked_missing}
            for incident_id, previous in missing:
                if incident_id in checked_ids:
                    continue
                current_incidents[incident_id] = previous
            next_detail_cursor = checked_missing[-1][0] if checked_missing else detail_cursor
        else:
            next_detail_cursor = ""

        next_state = {
            "version": 3,
            "initialized": True,
            "scope": scope,
            "incidents": current_incidents,
            "detail_cursor": next_detail_cursor,
        }
        return pollen, self._dump_state(next_state)

    @staticmethod
    def _incident_pollen(
        incident_id: str,
        incident: dict,
        pollen_type: str,
        discovered_at: str,
        transition_hash: str,
    ) -> dict:
        status = str(incident.get("status") or "unknown")
        title = str(incident.get("title") or "")
        urgency = str(incident.get("urgency") or "")
        return {
            "id": f"pagerduty-{incident_id}-{transition_hash}",
            "source": "pagerduty",
            "type": pollen_type,
            "title": title[:100],
            "preview": f"[{urgency}] {title}"[:200],
            "discovered_at": discovered_at,
            "author": str(incident.get("assignee_id") or ""),
            "author_name": str(incident.get("assignee_name") or ""),
            "group": str(incident.get("service_name") or "PagerDuty"),
            "url": str(incident.get("html_url") or ""),
            "metadata": {
                "incident_id": incident_id,
                "urgency": urgency,
                "service_name": str(incident.get("service_name") or ""),
                "status": status,
                "incident_number": incident.get("incident_number", 0),
            },
        }


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = PagerDutyScanner()
    if data.get("command") == "poll":
        result_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
