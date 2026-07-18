"""Linear scanner using cursor pagination and watermark-contained state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


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


def _canonical_timestamp(value: object) -> str | None:
    if not _bounded_text(value, 128, allow_empty=False):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _normalize_user(value: object, *, nullable: bool) -> dict | None:
    if value is None and nullable:
        return {"id": "", "name": "", "email": ""}
    if not isinstance(value, dict):
        return None
    if (
        not _bounded_text(value.get("id", ""), 128)
        or not _bounded_text(value.get("name", ""), 10_000)
        or not _bounded_text(value.get("email", ""), 320)
    ):
        return None
    return {
        "id": value.get("id", ""),
        "name": value.get("name", ""),
        "email": value.get("email", ""),
    }


def _valid_stored_issue(value: object) -> bool:
    priority = value.get("priority") if isinstance(value, dict) else None
    return (
        isinstance(value, dict)
        and _bounded_text(value.get("state"), 10_000)
        and isinstance(priority, int)
        and not isinstance(priority, bool)
        and 0 <= priority <= 4
        and _bounded_text(value.get("assignee_id"), 128)
        and _canonical_timestamp(value.get("created_at")) is not None
        and _canonical_timestamp(value.get("updated_at")) is not None
    )


def _normalize_node(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    internal_id = value.get("id")
    identifier = value.get("identifier")
    title = value.get("title")
    issue_state = value.get("state")
    priority = value.get("priority")
    if priority is None:
        priority = 0
    assignee = _normalize_user(value.get("assignee"), nullable=True)
    creator = _normalize_user(value.get("creator"), nullable=True)
    created_at = _canonical_timestamp(value.get("createdAt"))
    updated_at = _canonical_timestamp(value.get("updatedAt"))
    url = value.get("url", "")
    if (
        not _bounded_text(internal_id, 128, allow_empty=False)
        or not _bounded_text(identifier, 128, allow_empty=False)
        or not isinstance(title, str)
        or len(title) > 100_000
        or not isinstance(issue_state, dict)
        or not _bounded_text(issue_state.get("name"), 10_000)
        or not _bounded_text(issue_state.get("id", ""), 128)
        or isinstance(priority, bool)
        or not isinstance(priority, int)
        or not 0 <= priority <= 4
        or assignee is None
        or creator is None
        or created_at is None
        or updated_at is None
        or not _bounded_text(url, 2_000)
    ):
        return None
    return {
        "internal_id": internal_id,
        "identifier": identifier,
        "title": title,
        "state": issue_state["name"],
        "priority": priority,
        "assignee": assignee,
        "creator": creator,
        "created_at": created_at,
        "updated_at": updated_at,
        "url": url,
    }


class LinearScanner:
    name = "linear"
    MAX_CHANGED_ISSUES_PER_POLL = 500
    _POLL_BUDGET_SECONDS = 45

    def configure(self) -> dict:
        return {
            "enabled": False,
            "api_key_env": "LINEAR_API_KEY",
            "team_id": "",
            "assignee_id": "",
            "max_items": 50,
            "max_pages": 10,
            "overlap_minutes": 5,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _graphql(self, query: str, variables: dict, api_key: str) -> dict | None:
        payload = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            "https://api.linear.app/graphql",
            data=payload,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
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
                if not isinstance(result, dict) or result.get("errors"):
                    print("[linear] GraphQL returned errors", file=sys.stderr)
                    return None
                return result
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "")
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[linear] HTTP {exc.code}{suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[linear] API error: {exc}", file=sys.stderr)
        return None

    @staticmethod
    def _load_state(watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") in {2, 3}:
            state.setdefault("cursor", "")
            state.setdefault("pending_until", "")
            state.setdefault("pending_since", "")
            issues = state.get("issues")
            cursor = state["cursor"]
            pending_until = state["pending_until"]
            pending_since = state["pending_since"]
            if (
                not isinstance(state.get("initialized"), bool)
                or _canonical_timestamp(state.get("updated_at")) is None
                or not isinstance(issues, dict)
                or len(issues) > 500
                or not all(
                    _bounded_text(issue_id, 128, allow_empty=False)
                    and _valid_stored_issue(value)
                    for issue_id, value in issues.items()
                )
                or not _bounded_text(cursor, 2_000)
                or not _bounded_text(pending_until, 128)
                or not _bounded_text(pending_since, 128)
                or bool(cursor) != bool(pending_until and pending_since)
                or (
                    pending_until
                    and _canonical_timestamp(pending_until) is None
                )
                or (
                    pending_since
                    and _canonical_timestamp(pending_since) is None
                )
                or (
                    "scope" in state
                    and not (
                        isinstance(state["scope"], str)
                        and re.fullmatch(r"[0-9a-f]{16}", state["scope"])
                    )
                )
            ):
                return None
            state["updated_at"] = _canonical_timestamp(state["updated_at"])
            if pending_until:
                state["pending_until"] = _canonical_timestamp(pending_until)
                state["pending_since"] = _canonical_timestamp(pending_since)
            for value in issues.values():
                value["created_at"] = _canonical_timestamp(value["created_at"])
                value["updated_at"] = _canonical_timestamp(value["updated_at"])
            state["version"] = 3
            return state
        if isinstance(watermark, str) and watermark.lstrip().startswith(("{", "[")):
            return None
        legacy = watermark if isinstance(watermark, str) and watermark else "1970-01-01T00:00:00Z"
        canonical_legacy = _canonical_timestamp(legacy)
        if canonical_legacy is None:
            return None
        return {
            "version": 3,
            "updated_at": canonical_legacy,
            "initialized": not canonical_legacy.startswith("1970-"),
            "issues": {},
        }

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        api_key_env = config.get("api_key_env", "LINEAR_API_KEY")
        if not isinstance(api_key_env, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", api_key_env
        ):
            return [], watermark
        api_key = os.environ.get(api_key_env, "")
        if (
            not api_key
            or len(api_key) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in api_key)
        ):
            return [], watermark
        scan_started_at = self._utc_now_z()
        team_id = config.get("team_id", "")
        assignee_id = config.get("assignee_id", "")
        if (
            not isinstance(team_id, str)
            or not isinstance(assignee_id, str)
            or team_id != team_id.strip()
            or assignee_id != assignee_id.strip()
        ):
            return [], watermark
        page_size = config.get("max_items", 50)
        max_pages = config.get("max_pages", 10)
        overlap = config.get("overlap_minutes", 5)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (page_size, max_pages, overlap)
            )
            or not 1 <= page_size <= 250
            or not 1 <= max_pages <= 10
            or not 0 <= overlap <= 1440
        ):
            print("[linear] pagination limits are invalid", file=sys.stderr)
            return [], watermark
        if (
            len(team_id) > 128
            or len(assignee_id) > 128
            or any(
                ord(char) < 32 or ord(char) == 127
                for char in f"{team_id}{assignee_id}"
            )
        ):
            return [], watermark

        scope = hashlib.sha256(
            json.dumps(
                {"team_id": team_id, "assignee_id": assignee_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]
        state = self._load_state(watermark)
        if state is None:
            print("[linear] invalid persisted state; preserving watermark", file=sys.stderr)
            return [], watermark
        same_scope = state.get("scope") == scope
        initialized = bool(state.get("initialized")) and same_scope
        since = state["updated_at"]
        raw_cursor = state.get("cursor") if initialized else ""
        if raw_cursor in (None, ""):
            cursor = ""
        elif (
            not isinstance(raw_cursor, str)
            or len(raw_cursor) > 2000
            or any(ord(char) < 32 or ord(char) == 127 for char in raw_cursor)
        ):
            return [], watermark
        else:
            cursor = raw_cursor
        if cursor:
            pending_until = str(state.get("pending_until") or "")
            effective_since = str(state.get("pending_since") or "")
            if not pending_until or not effective_since:
                return [], watermark
        else:
            pending_until = scan_started_at
            if initialized:
                try:
                    effective_since = (
                        datetime.fromisoformat(since.replace("Z", "+00:00"))
                        - timedelta(minutes=overlap)
                    ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except (TypeError, ValueError):
                    return [], watermark
            else:
                effective_since = ""

        filters: list[str] = []
        definitions = ["$first: Int!", "$after: String", "$until: DateTime!"]
        updated_conditions = ["lte: $until"]
        if initialized:
            definitions.append("$since: DateTime!")
            updated_conditions.append("gte: $since")
        filters.append(f"updatedAt: {{ {', '.join(updated_conditions)} }}")
        if team_id:
            definitions.append("$teamId: ID!")
            filters.append("team: { id: { eq: $teamId } }")
        if assignee_id:
            definitions.append("$assigneeId: ID!")
            filters.append("assignee: { id: { eq: $assigneeId } }")
        filter_text = f"filter: {{ {', '.join(filters)} }}" if filters else ""
        query = f"""
        query({', '.join(definitions)}) {{
          issues({filter_text} first: $first after: $after orderBy: updatedAt) {{
            nodes {{
              id identifier title priority createdAt updatedAt url
              state {{ id name }}
              assignee {{ id name email }}
              creator {{ id name email }}
            }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """
        variables: dict[str, object] = {
            "first": page_size,
            "after": cursor or None,
            "until": pending_until,
        }
        if initialized:
            variables["since"] = effective_since
        if team_id:
            variables["teamId"] = team_id
        if assignee_id:
            variables["assigneeId"] = assignee_id

        nodes: list[dict] = []
        pages_to_fetch = max_pages if initialized else 1
        continuation_cursor = cursor
        seen_cursors = {cursor} if cursor else set()
        for page in range(pages_to_fetch):
            result = self._graphql(query, variables, api_key)
            if result is None:
                return [], watermark
            data = result.get("data")
            if not isinstance(data, dict):
                return [], watermark
            connection = data.get("issues")
            if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
                return [], watermark
            page_nodes = connection["nodes"]
            if not all(isinstance(value, dict) for value in page_nodes):
                return [], watermark
            nodes.extend(page_nodes)
            if len(nodes) > self.MAX_CHANGED_ISSUES_PER_POLL:
                print("[linear] changed issue set exceeded safe state capacity", file=sys.stderr)
                return [], watermark
            page_info = connection.get("pageInfo", {})
            if (
                not isinstance(page_info, dict)
                or not isinstance(page_info.get("hasNextPage"), bool)
            ):
                return [], watermark
            if not page_info["hasNextPage"]:
                break
            raw_end_cursor = page_info.get("endCursor")
            if (
                not isinstance(raw_end_cursor, str)
                or not raw_end_cursor
                or len(raw_end_cursor) > 2000
                or any(
                    ord(char) < 32 or ord(char) == 127
                    for char in raw_end_cursor
                )
            ):
                return [], watermark
            if raw_end_cursor in seen_cursors:
                print("[linear] repeated GraphQL page cursor", file=sys.stderr)
                return [], watermark
            seen_cursors.add(raw_end_cursor)
            continuation_cursor = raw_end_cursor
            if page + 1 >= pages_to_fetch:
                if not initialized:
                    # Bootstrap snapshots only a bounded current page.
                    continuation_cursor = ""
                break
            variables["after"] = continuation_cursor
        else:
            continuation_cursor = ""

        if not page_info.get("hasNextPage"):
            continuation_cursor = ""

        committed_issues = state.get("issues", {}) if same_scope else {}
        next_issues = dict(committed_issues)
        pollen: list[dict] = []
        seen_issue_ids: set[str] = set()
        normalized_nodes: list[dict] = []
        for node in nodes:
            normalized = _normalize_node(node)
            if (
                normalized is None
                or normalized["identifier"] in seen_issue_ids
            ):
                print("[linear] malformed issue node", file=sys.stderr)
                return [], watermark
            issue_id = normalized["identifier"]
            seen_issue_ids.add(issue_id)
            normalized_nodes.append(normalized)

        for node in normalized_nodes:
            issue_id = node["identifier"]
            current = {
                "state": node["state"],
                "priority": node["priority"],
                "assignee_id": node["assignee"]["id"],
                "created_at": node["created_at"],
                "updated_at": node["updated_at"],
            }
            previous = committed_issues.get(issue_id)
            next_issues[issue_id] = current
            if not initialized or previous == current:
                continue
            if not isinstance(previous, dict):
                if assignee_id and current["assignee_id"] == assignee_id:
                    pollen_type = "issue_assigned"
                elif current["created_at"] and current["created_at"] >= since:
                    pollen_type = "linear_issue_new"
                else:
                    pollen_type = "issue_updated"
            elif assignee_id and previous.get("assignee_id") != assignee_id and current["assignee_id"] == assignee_id:
                pollen_type = "issue_assigned"
            else:
                pollen_type = "issue_updated"
            transition = json.dumps({"previous": previous, "current": current}, sort_keys=True)
            transition_hash = hashlib.sha256(transition.encode()).hexdigest()[:10]
            title = node["title"]
            pollen.append({
                "id": f"linear-{issue_id}-{transition_hash}",
                "source": "linear",
                "type": pollen_type,
                "title": f"{issue_id}: {title}"[:100],
                "preview": f"[{current['state']}] {title}"[:200],
                "discovered_at": scan_started_at,
                "author": node["creator"]["email"] or node["creator"]["id"],
                "author_name": node["creator"]["name"],
                "group": "Issues",
                "url": node["url"],
                "metadata": {
                    "identifier": issue_id,
                    "state": current["state"],
                    "priority": current["priority"],
                    "assignee_id": current["assignee_id"],
                    "previous": previous,
                },
            })

        if len(next_issues) > 500:
            next_issues = dict(
                sorted(
                    next_issues.items(),
                    key=lambda pair: str(pair[1].get("updated_at", "")) if isinstance(pair[1], dict) else "",
                )[-500:]
            )
        next_state = {
            "version": 3,
            "updated_at": since if continuation_cursor else pending_until,
            "initialized": True,
            "scope": scope,
            "issues": next_issues,
            "cursor": continuation_cursor,
            "pending_until": pending_until if continuation_cursor else "",
            "pending_since": effective_since if continuation_cursor else "",
        }
        return pollen, self._dump_state(next_state)


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = LinearScanner()
    if data.get("command") == "poll":
        poll_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": poll_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
