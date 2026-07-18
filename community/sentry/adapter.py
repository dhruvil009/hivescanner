"""Sentry scanner using the organization issues endpoint and count deltas."""

from __future__ import annotations

import hashlib
import json
import math
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


def _parse_count(value: object) -> int | None:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or not 1 <= len(value) <= 20
    ):
        return None
    parsed = int(value)
    return parsed if parsed <= 10**18 else None


def _normalize_issue(value: object) -> tuple[str, dict] | None:
    if not isinstance(value, dict):
        return None
    issue_id = value.get("id")
    count = _parse_count(value.get("count"))
    fields = {
        "last_seen": (value.get("lastSeen"), 128, False),
        "title": (value.get("title"), 10_000, True),
        "short_id": (value.get("shortId", ""), 128, True),
        "level": (value.get("level", ""), 64, True),
        "platform": (value.get("platform", ""), 128, True),
        "permalink": (value.get("permalink", ""), 2_000, True),
    }
    if (
        not _bounded_text(issue_id, 128, allow_empty=False)
        or count is None
        or not all(
            _bounded_text(field, maximum, allow_empty=allow_empty)
            for field, maximum, allow_empty in fields.values()
        )
    ):
        return None
    return issue_id, {
        "count": count,
        "last_seen": fields["last_seen"][0],
        "title": fields["title"][0][:500],
        "short_id": fields["short_id"][0],
        "level": fields["level"][0],
        "platform": fields["platform"][0],
        "permalink": fields["permalink"][0][:1000],
    }


def _valid_stored_issue(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("count"), int)
        and not isinstance(value.get("count"), bool)
        and 0 <= value["count"] <= 10**18
        and isinstance(value.get("alert_count"), int)
        and not isinstance(value.get("alert_count"), bool)
        and 0 <= value["alert_count"] <= 10**18
        and _bounded_text(value.get("last_seen"), 128, allow_empty=False)
    )


def _normalize_terminal_detail(detail: dict, previous: dict) -> dict | None:
    count = previous["count"]
    if "count" in detail:
        count = _parse_count(detail["count"])
        if count is None:
            return None
    last_seen = previous["last_seen"]
    if "lastSeen" in detail:
        if not _bounded_text(detail["lastSeen"], 128, allow_empty=False):
            return None
        last_seen = detail["lastSeen"]
    normalized = {"count": count, "last_seen": last_seen}
    for source, target, maximum in (
        ("title", "title", 500),
        ("shortId", "short_id", 128),
        ("level", "level", 64),
        ("platform", "platform", 128),
        ("permalink", "permalink", 1_000),
    ):
        value = detail.get(source, "")
        if value is None:
            value = ""
        if not _bounded_text(value, maximum):
            return None
        normalized[target] = value
    return normalized


class SentryScanner:
    name = "sentry"
    _POLL_BUDGET_SECONDS = 45
    MAX_TRACKED_ISSUES = 500
    MAX_DETAIL_CHECKS_PER_POLL = 10

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "SENTRY_TOKEN",
            "organization": "",
            "project": "",
            "query": "is:unresolved",
            "min_event_delta": 10,
            "spike_ratio": 2.0,
            "max_items": 100,
            "max_pages": 10,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(
        self, path: str, token: str, params: Optional[dict] = None
    ) -> tuple[list[dict] | None, str]:
        query = urllib.parse.urlencode(params or {})
        url = f"https://sentry.io/api/0{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        deadline = getattr(self, "_poll_deadline", None)
        timeout = 15.0
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, ""
            timeout = min(timeout, max(0.1, remaining))
        try:
            with _urlopen(req, timeout=timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("response exceeded 2 MB")
                data = _strict_json(raw)
                if not isinstance(data, list) or not all(isinstance(value, dict) for value in data):
                    return None, ""
                next_cursor = ""
                link = response.headers.get("Link", "")
                for segment in link.split(","):
                    if 'rel="next"' not in segment or 'results="true"' not in segment:
                        continue
                    match = re.search(r"<([^>]+)>", segment)
                    if not match:
                        raise ValueError("malformed next-page Link header")
                    parsed = urllib.parse.urlsplit(match.group(1))
                    cursor_values = urllib.parse.parse_qs(
                        parsed.query, keep_blank_values=True
                    ).get("cursor", [])
                    if (
                        len(cursor_values) != 1
                        or not _bounded_text(
                            cursor_values[0], 2_000, allow_empty=False
                        )
                    ):
                        raise ValueError("invalid next-page cursor")
                    next_cursor = cursor_values[0]
                    break
                return data, next_cursor
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "")
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[sentry] HTTP {exc.code}{suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[sentry] API error: {exc}", file=sys.stderr)
        return None, ""

    def _api_object(self, path: str, token: str) -> dict | None:
        req = urllib.request.Request(
            f"https://sentry.io/api/0{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
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
                value = _strict_json(raw)
                return value if isinstance(value, dict) else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"_not_found": True}
            retry_after = exc.headers.get("Retry-After", "")
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[sentry] HTTP {exc.code}{suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[sentry] API error: {exc}", file=sys.stderr)
        return None

    @staticmethod
    def _load_state(watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") in {2, 3}:
            issues = state.get("issues")
            if (
                not isinstance(state.get("initialized"), bool)
                or not isinstance(issues, dict)
                or len(issues) > SentryScanner.MAX_TRACKED_ISSUES
                or not all(
                    _bounded_text(issue_id, 128, allow_empty=False)
                    and _valid_stored_issue(value)
                    for issue_id, value in issues.items()
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
            "issues": {},
        }

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        token_env = config.get("token_env", "SENTRY_TOKEN")
        if not isinstance(token_env, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", token_env
        ):
            return [], watermark
        token = os.environ.get(token_env, "")
        organization = config.get("organization", "")
        if (
            not token
            or len(token) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in token)
            or not isinstance(organization, str)
            or not organization
        ):
            return [], watermark
        page_size = config.get("max_items", 100)
        max_pages = config.get("max_pages", 10)
        min_delta = config.get("min_event_delta", 10)
        spike_ratio = config.get("spike_ratio", 2.0)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (page_size, max_pages, min_delta)
            )
            or not 1 <= page_size <= 100
            or not 1 <= max_pages <= 10
            or not 1 <= min_delta <= 1_000_000_000
            or isinstance(spike_ratio, bool)
            or not isinstance(spike_ratio, (int, float))
            or not math.isfinite(spike_ratio)
            or not 1.0 <= spike_ratio <= 1_000_000.0
        ):
            print("[sentry] thresholds or pagination limits are invalid", file=sys.stderr)
            return [], watermark
        project = config.get("project", "")
        if (
            not isinstance(project, str)
            or len(organization) > 128
            or len(project) > 128
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", organization)
            or (project and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project))
        ):
            print("[sentry] organization/project must be valid slugs", file=sys.stderr)
            return [], watermark
        configured_query = config.get("query", "is:unresolved")
        if (
            not isinstance(configured_query, str)
            or len(configured_query) > 5000
            or any(ord(char) < 32 or ord(char) == 127 for char in configured_query)
        ):
            return [], watermark
        # Always add the configured project as an outer AND constraint. Merely
        # noticing some `project:` token is unsafe: a query scoped to another
        # project (or an OR expression) must not override this setting.
        if project:
            configured_query = f"({configured_query}) project:{project}"
        scope = hashlib.sha256(
            json.dumps(
                {
                    "organization": organization,
                    "project": project,
                    "query": configured_query,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]

        state = self._load_state(watermark)
        if state is None:
            print("[sentry] invalid persisted state; preserving watermark", file=sys.stderr)
            return [], watermark

        encoded_org = urllib.parse.quote(organization, safe="")
        path = f"/organizations/{encoded_org}/issues/"
        issues: list[dict] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for page in range(max_pages):
            params = {"query": configured_query, "sort": "date", "limit": page_size}
            if cursor:
                params["cursor"] = cursor
            page_issues, next_cursor = self._api(path, token, params)
            if page_issues is None:
                return [], watermark
            issues.extend(page_issues)
            if len(issues) > self.MAX_TRACKED_ISSUES:
                print(
                    f"[sentry] more than {self.MAX_TRACKED_ISSUES} matching issues; narrow the query or use webhooks",
                    file=sys.stderr,
                )
                return [], watermark
            if next_cursor and not _bounded_text(
                next_cursor, 2_000, allow_empty=False
            ):
                print("[sentry] invalid pagination cursor", file=sys.stderr)
                return [], watermark
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                print("[sentry] repeated pagination cursor", file=sys.stderr)
                return [], watermark
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            if page + 1 >= max_pages:
                print("[sentry] unresolved issues exceeded max_pages; use webhooks", file=sys.stderr)
                return [], watermark

        same_scope = state.get("scope") == scope
        initialized = bool(state.get("initialized")) and same_scope
        previous_issues = state.get("issues", {}) if same_scope else {}
        current_issues: dict[str, dict] = {}
        pollen: list[dict] = []
        discovered_at = self._utc_now_z()

        normalized_issues: list[tuple[str, dict]] = []
        seen_issue_ids: set[str] = set()
        for issue in issues:
            normalized = _normalize_issue(issue)
            if normalized is None or normalized[0] in seen_issue_ids:
                print("[sentry] malformed issue page", file=sys.stderr)
                return [], watermark
            seen_issue_ids.add(normalized[0])
            normalized_issues.append(normalized)

        for issue_id, current in normalized_issues:
            count = current["count"]
            previous = previous_issues.get(issue_id)
            previous_alert_count = (
                previous["alert_count"] if isinstance(previous, dict) else count
            )
            alert_count = previous_alert_count
            pollen_type = ""
            if initialized and not isinstance(previous, dict):
                pollen_type = "sentry_issue"
                alert_count = count
            elif initialized and isinstance(previous, dict):
                delta = max(0, count - previous_alert_count)
                ratio = count / max(1, previous_alert_count)
                if delta >= min_delta and ratio >= spike_ratio:
                    pollen_type = "sentry_spike"
                    alert_count = count
            current_issues[issue_id] = {
                "count": count,
                "alert_count": alert_count,
                "last_seen": current["last_seen"],
            }
            if not pollen_type:
                continue
            transition = f"{pollen_type}:{previous_alert_count}:{count}:{current['last_seen']}"
            transition_hash = hashlib.sha256(transition.encode()).hexdigest()[:10]
            pollen.append(self._pollen(issue_id, current, pollen_type, transition_hash, project or organization, discovered_at))

        if initialized:
            missing = sorted([
                (issue_id, previous)
                for issue_id, previous in previous_issues.items()
                if issue_id not in current_issues and isinstance(previous, dict)
            ], key=lambda value: value[0])
            detail_cursor = state.get("detail_cursor", "")
            split_at = next(
                (
                    index for index, (issue_id, _) in enumerate(missing)
                    if issue_id > detail_cursor
                ),
                0,
            )
            ordered_missing = [*missing[split_at:], *missing[:split_at]]
            checked_missing = ordered_missing[: self.MAX_DETAIL_CHECKS_PER_POLL]
            for issue_id, previous in checked_missing:
                detail = self._api_object(
                    f"/organizations/{encoded_org}/issues/"
                    f"{urllib.parse.quote(issue_id, safe='')}/",
                    token,
                )
                if detail is None:
                    current_issues[issue_id] = previous
                    continue
                if detail.get("_not_found") is True:
                    pollen_type = "sentry_no_longer_matching"
                    status = "deleted"
                    detail = {}
                else:
                    status = detail.get("status")
                    if isinstance(status, str) and status in {
                        "resolved",
                        "resolvedInNextRelease",
                    }:
                        pollen_type = "sentry_resolved"
                    elif isinstance(status, str) and status in {"ignored", "muted"}:
                        pollen_type = "sentry_ignored"
                    else:
                        # An unresolved detail can temporarily disappear from
                        # the list during index propagation. Arbitrary Sentry
                        # search syntax cannot be safely re-evaluated here, so
                        # retain it until a terminal status is proven.
                        current_issues[issue_id] = previous
                        continue
                terminal = _normalize_terminal_detail(detail, previous)
                if terminal is None:
                    current_issues[issue_id] = previous
                    continue
                terminal["status"] = status
                transition_hash = hashlib.sha256(
                    f"{pollen_type}:{status}:{terminal['last_seen']}".encode()
                ).hexdigest()[:10]
                pollen.append(
                    self._pollen(
                        issue_id,
                        terminal,
                        pollen_type,
                        transition_hash,
                        project or organization,
                        discovered_at,
                    )
                )
            checked_ids = {issue_id for issue_id, _ in checked_missing}
            for issue_id, previous in missing:
                if issue_id in checked_ids:
                    continue
                current_issues[issue_id] = previous
            next_detail_cursor = checked_missing[-1][0] if checked_missing else detail_cursor
        else:
            next_detail_cursor = ""

        next_state = {
            "version": 3,
            "initialized": True,
            "scope": scope,
            "issues": current_issues,
            "detail_cursor": next_detail_cursor,
        }
        return pollen, self._dump_state(next_state)

    @staticmethod
    def _pollen(
        issue_id: str,
        issue: dict,
        pollen_type: str,
        transition_hash: str,
        group: str,
        discovered_at: str,
    ) -> dict:
        title = str(issue.get("title") or "")
        short_id = str(issue.get("short_id") or "")
        return {
            "id": f"sentry-{issue_id}-{transition_hash}",
            "source": "sentry",
            "type": pollen_type,
            "title": f"{short_id}: {title}"[:100] if short_id else title[:100],
            "preview": f"[{issue.get('level', '')}] {title}"[:200],
            "discovered_at": discovered_at,
            "author": "",
            "author_name": "",
            "group": group,
            "url": str(issue.get("permalink") or ""),
            "metadata": {
                "issue_id": issue_id,
                "level": str(issue.get("level") or ""),
                "platform": str(issue.get("platform") or ""),
                "count": issue.get("count", 0),
                "last_seen": str(issue.get("last_seen") or ""),
            },
        }


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = SentryScanner()
    if data.get("command") == "poll":
        result_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
