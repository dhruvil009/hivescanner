"""Jira Cloud scanner using the current enhanced JQL search endpoint."""

from __future__ import annotations

import base64
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
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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


def _valid_timestamp(value: object) -> bool:
    if not _bounded_text(value, 128, allow_empty=False):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _normalize_user(value: object, *, nullable: bool) -> dict | None:
    if value is None and nullable:
        return {"account_id": "", "display_name": "", "email": ""}
    if not isinstance(value, dict):
        return None
    account_id = value.get("accountId", "")
    display_name = value.get("displayName", "")
    email = value.get("emailAddress", "")
    if (
        not _bounded_text(account_id, 256)
        or not _bounded_text(display_name, 10_000)
        or not _bounded_text(email, 320)
    ):
        return None
    return {
        "account_id": account_id,
        "display_name": display_name,
        "email": email,
    }


def _named_field(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return ""
    if not isinstance(value, dict) or not _bounded_text(value.get("name"), 10_000):
        return None
    return value["name"]


def _valid_stored_issue(value: object) -> bool:
    return (
        isinstance(value, dict)
        and _valid_timestamp(value.get("updated"))
        and _bounded_text(value.get("assignee_id"), 256)
        and isinstance(value.get("mentioned"), bool)
        and isinstance(value.get("description_hash"), str)
        and re.fullmatch(r"[0-9a-f]{16}", value["description_hash"]) is not None
        and _bounded_text(value.get("status"), 10_000)
    )


def _adf_to_text(node: object) -> str:
    """Flatten a bounded subset of Atlassian Document Format to plain text."""
    parts: list[str] = []

    def visit(value: object, depth: int = 0) -> None:
        if depth > 20 or len(parts) >= 1000:
            return
        if isinstance(value, str):
            parts.append(value)
            return
        if not isinstance(value, dict):
            return
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            parts.append(value["text"])
        elif value.get("type") == "mention" and isinstance(value.get("attrs"), dict):
            for key in ("text", "id"):
                if isinstance(value["attrs"].get(key), str):
                    parts.append(value["attrs"][key])
        children = value.get("content")
        if isinstance(children, list):
            for child in children[:1000]:
                visit(child, depth + 1)

    visit(node)
    return " ".join(parts)[:100_000]


def _adf_mentions_account(node: object, account_id: str) -> bool:
    """Find an exact Jira ADF mention without treating arbitrary text as one."""
    if not account_id:
        return False
    remaining = [node]
    visited = 0
    while remaining and visited < 20_000:
        value = remaining.pop()
        visited += 1
        if not isinstance(value, dict):
            continue
        attrs = value.get("attrs") if isinstance(value.get("attrs"), dict) else {}
        if (
            value.get("type") == "mention"
            and isinstance(attrs.get("id"), str)
            and attrs["id"] == account_id
        ):
            return True
        children = value.get("content")
        if isinstance(children, list):
            remaining.extend(children[:1000])
    return False


class JiraScanner:
    name = "jira"
    _POLL_BUDGET_SECONDS = 45

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "JIRA_TOKEN",
            "domain": "",
            "username": "",
            "account_id": "",
            "jql": "assignee = currentUser() OR watcher = currentUser()",
            "mention_terms": [],
            "jira_timezone": "UTC",
            "overlap_minutes": 10,
            "max_items": 100,
            "max_pages": 10,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _valid_domain(value: object) -> str | None:
        if not isinstance(value, str) or value != value.strip():
            return None
        raw = value.lower()
        parsed = urllib.parse.urlsplit(f"https://{raw}")
        try:
            port = parsed.port
        except ValueError:
            return None
        if (
            not raw
            or parsed.hostname != raw
            or port is not None
            or parsed.username
            or parsed.password
            or "/" in raw
            or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.atlassian\.net",
                raw,
            )
        ):
            return None
        return raw

    def _api(
        self,
        path: str,
        domain: str,
        username: str,
        token: str,
        params: Optional[dict] = None,
    ) -> Optional[dict]:
        query = urllib.parse.urlencode(params or {})
        url = f"https://{domain}/rest/api/3/{path}" + (f"?{query}" if query else "")
        credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"},
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
                data = _strict_json(raw)
                return data if isinstance(data, dict) else None
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "")
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[jira] HTTP {exc.code}{suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[jira] API error: {exc}", file=sys.stderr)
        return None

    @staticmethod
    def _load_state(watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") in {2, 3, 4}:
            state.setdefault("next_page_token", "")
            state.setdefault("pending_since_jql", "")
            state.setdefault("pending_until", "")
            issues = state.get("issues")
            token = state["next_page_token"]
            since_jql = state["pending_since_jql"]
            pending_until = state["pending_until"]
            if (
                not isinstance(state.get("initialized"), bool)
                or not _valid_timestamp(state.get("updated_at"))
                or not isinstance(issues, dict)
                or len(issues) > 500
                or not all(
                    _bounded_text(key, 255, allow_empty=False)
                    and _valid_stored_issue(value)
                    for key, value in issues.items()
                )
                or not _bounded_text(token, 2_000)
                or not _bounded_text(since_jql, 64)
                or not _bounded_text(pending_until, 128)
                or bool(token) != bool(since_jql and pending_until)
                or (pending_until and not _valid_timestamp(pending_until))
                or (
                    "scope" in state
                    and not (
                        isinstance(state["scope"], str)
                        and re.fullmatch(r"[0-9a-f]{16}", state["scope"])
                    )
                )
            ):
                return None
            state["version"] = 4
            return state
        if isinstance(watermark, str) and watermark.lstrip().startswith(("{", "[")):
            return None
        legacy = watermark if isinstance(watermark, str) and watermark else "1970-01-01T00:00:00Z"
        if not _valid_timestamp(legacy):
            return None
        return {
            "version": 4,
            "updated_at": legacy,
            "initialized": not legacy.startswith("1970-"),
            "issues": {},
        }

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        token_env = config.get("token_env", "JIRA_TOKEN")
        if not isinstance(token_env, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", token_env
        ):
            return [], watermark
        token = os.environ.get(token_env, "")
        domain = self._valid_domain(config.get("domain"))
        username = config.get("username", "")
        if (
            not token
            or len(token) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in token)
            or not domain
            or not isinstance(username, str)
            or not username
            or username != username.strip()
            or len(username) > 320
            or any(ord(char) < 32 or ord(char) == 127 for char in username)
        ):
            return [], watermark
        page_size = config.get("max_items", 100)
        max_pages = config.get("max_pages", 10)
        overlap = config.get("overlap_minutes", 10)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (page_size, max_pages, overlap)
            )
            or not 1 <= page_size <= 100
            or not 1 <= max_pages <= 10
            or not 1 <= overlap <= 1440
        ):
            print("[jira] pagination limits are invalid", file=sys.stderr)
            return [], watermark
        timezone_name = config.get("jira_timezone", "UTC")
        if (
            not isinstance(timezone_name, str)
            or not timezone_name
            or len(timezone_name) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in timezone_name)
        ):
            return [], watermark
        try:
            jira_zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            print(f"[jira] unknown jira_timezone: {timezone_name}", file=sys.stderr)
            return [], watermark

        base_jql = config.get(
            "jql", "assignee = currentUser() OR watcher = currentUser()"
        )
        if (
            not isinstance(base_jql, str)
            or not base_jql
            or base_jql != base_jql.strip()
            or len(base_jql) > 5000
            or any(ord(char) < 32 or ord(char) == 127 for char in base_jql)
        ):
            return [], watermark
        account_id = config.get("account_id", "")
        if not isinstance(account_id, str) or account_id != account_id.strip() or len(account_id) > 256 or any(
            ord(char) < 32 or ord(char) == 127 for char in account_id
        ):
            return [], watermark
        mention_terms_raw = config.get("mention_terms", [])
        if (
            not isinstance(mention_terms_raw, list)
            or len(mention_terms_raw) > 100
            or not all(isinstance(value, str) for value in mention_terms_raw)
        ):
            return [], watermark
        if any(
            not value
            or value != value.strip()
            or len(value) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in mention_terms_raw
        ):
            return [], watermark
        mention_terms = sorted({
            value.casefold() for value in mention_terms_raw
        })
        scope = hashlib.sha256(
            json.dumps(
                {
                    "domain": domain,
                    "username": username,
                    "jql": base_jql,
                    "account_id": account_id,
                    "mention_terms": sorted(mention_terms),
                    "jira_timezone": timezone_name,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]
        scan_started_at = self._utc_now_z()
        state = self._load_state(watermark)
        if state is None:
            print("[jira] invalid persisted state; preserving watermark", file=sys.stderr)
            return [], watermark
        same_scope = state.get("scope") == scope
        initialized = bool(state.get("initialized")) and same_scope
        page_token = state.get("next_page_token", "") if initialized else ""
        if initialized:
            if page_token:
                since_jql = str(state.get("pending_since_jql") or "")
                pending_until = str(state.get("pending_until") or "")
                if not since_jql or not pending_until:
                    return [], watermark
            else:
                try:
                    since = datetime.fromisoformat(
                        str(state.get("updated_at")).replace("Z", "+00:00")
                    )
                    if since.tzinfo is None:
                        since = since.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    return [], watermark
                since = since.astimezone(jira_zone) - timedelta(minutes=overlap)
                since_jql = since.strftime("%Y-%m-%d %H:%M")
                pending_until = scan_started_at
            try:
                until_jql = datetime.fromisoformat(
                    pending_until.replace("Z", "+00:00")
                ).astimezone(jira_zone).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                return [], watermark
            time_clause = (
                f'updated >= "{since_jql}" AND updated <= "{until_jql}"'
            )
        else:
            # Bootstrap a bounded current view instead of querying from 1970.
            pending_until = scan_started_at
            until_jql = datetime.fromisoformat(
                pending_until.replace("Z", "+00:00")
            ).astimezone(jira_zone).strftime("%Y-%m-%d %H:%M")
            since_jql = ""
            time_clause = f'updated >= -1d AND updated <= "{until_jql}"'
        sort_direction = "ASC" if initialized else "DESC"
        jql = (
            f"({base_jql}) AND {time_clause} "
            f"ORDER BY updated {sort_direction}"
        )
        fields = "summary,status,priority,issuetype,assignee,creator,description,updated"

        issues: list[dict] = []
        next_page_token = page_token
        seen_page_tokens = {page_token} if page_token else set()
        pages_to_fetch = max_pages if initialized else 1
        for page in range(pages_to_fetch):
            params = {
                "jql": jql,
                "maxResults": str(page_size),
                "fields": fields,
            }
            if next_page_token:
                params["nextPageToken"] = next_page_token
            result = self._api("search/jql", domain, username, token, params)
            if result is None or not isinstance(result.get("issues"), list):
                return [], watermark
            page_issues = result["issues"]
            if not all(isinstance(value, dict) for value in page_issues):
                print("[jira] malformed enhanced-search issue page", file=sys.stderr)
                return [], watermark
            issues.extend(page_issues)
            raw_page_token = result.get("nextPageToken")
            if raw_page_token in (None, ""):
                next_page_token = ""
            elif (
                not isinstance(raw_page_token, str)
                or len(raw_page_token) > 2000
                or any(ord(char) < 32 or ord(char) == 127 for char in raw_page_token)
            ):
                return [], watermark
            else:
                next_page_token = raw_page_token
            is_last = result.get("isLast")
            if not isinstance(is_last, bool):
                print("[jira] enhanced search omitted isLast", file=sys.stderr)
                return [], watermark
            if is_last:
                if next_page_token:
                    print(
                        "[jira] enhanced search returned a token on the last page",
                        file=sys.stderr,
                    )
                    return [], watermark
                next_page_token = ""
                break
            if not next_page_token:
                print(
                    "[jira] enhanced search reported more results without a token",
                    file=sys.stderr,
                )
                return [], watermark
            if next_page_token in seen_page_tokens:
                print("[jira] repeated enhanced-search page token", file=sys.stderr)
                return [], watermark
            seen_page_tokens.add(next_page_token)
            if page + 1 >= pages_to_fetch:
                if not initialized:
                    # Bootstrap snapshots only a bounded recent page.
                    next_page_token = ""
                break

        committed_issues = state.get("issues", {}) if same_scope else {}
        next_issues = dict(committed_issues)
        pollen: list[dict] = []
        seen_issue_keys: set[str] = set()
        normalized_issues: list[dict] = []
        for issue in issues:
            key = issue.get("key")
            fields_obj = issue.get("fields")
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 255
                or any(ord(char) < 32 or ord(char) == 127 for char in key)
                or key in seen_issue_keys
                or not isinstance(fields_obj, dict)
            ):
                print("[jira] malformed issue in enhanced-search response", file=sys.stderr)
                return [], watermark
            seen_issue_keys.add(key)
            summary = fields_obj.get("summary")
            status_name = _named_field(fields_obj.get("status"))
            priority_name = _named_field(fields_obj.get("priority"), nullable=True)
            type_name = _named_field(fields_obj.get("issuetype"))
            assignee = _normalize_user(fields_obj.get("assignee"), nullable=True)
            creator = _normalize_user(fields_obj.get("creator"), nullable=True)
            description = fields_obj.get("description")
            raw_updated = fields_obj.get("updated")
            if (
                not isinstance(summary, str)
                or len(summary) > 100_000
                or status_name is None
                or priority_name is None
                or type_name is None
                or assignee is None
                or creator is None
                or not isinstance(description, (dict, str, type(None)))
                or not _valid_timestamp(raw_updated)
            ):
                print("[jira] malformed issue fields", file=sys.stderr)
                return [], watermark
            normalized_issues.append(
                {
                    "key": key,
                    "summary": summary,
                    "status": status_name,
                    "priority": priority_name,
                    "issue_type": type_name,
                    "assignee": assignee,
                    "creator": creator,
                    "description": description,
                    "updated": raw_updated,
                }
            )

        for issue in normalized_issues:
            key = issue["key"]
            summary = issue["summary"]
            description = issue["description"]
            description_text = _adf_to_text(description)
            assignee_value = issue["assignee"]["account_id"]
            mentioned = _adf_mentions_account(description, account_id) or any(
                re.search(
                    rf"(?<!\w){re.escape(term)}(?!\w)",
                    description_text,
                    re.IGNORECASE,
                )
                is not None
                for term in mention_terms
            )
            updated = issue["updated"]
            current = {
                "updated": updated,
                "assignee_id": assignee_value,
                "mentioned": mentioned,
                "description_hash": hashlib.sha256(description_text.encode()).hexdigest()[:16],
                "status": issue["status"],
            }
            previous = committed_issues.get(key)
            next_issues[key] = current
            if not initialized or previous == current:
                continue
            if (
                account_id
                and (
                    not isinstance(previous, dict)
                    or previous.get("assignee_id") != account_id
                )
                and assignee_value == account_id
            ):
                pollen_type = "jira_assigned"
            elif (
                mentioned
                and (
                    not isinstance(previous, dict)
                    or not previous.get("mentioned")
                )
            ):
                pollen_type = "jira_mentioned"
            else:
                pollen_type = "jira_updated"
            transition_hash = hashlib.sha256(
                json.dumps(
                    {"previous": previous, "current": current},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:10]
            pollen.append({
                "id": f"jira-{key}-{transition_hash}",
                "source": "jira",
                "type": pollen_type,
                "title": f"{key}: {summary}"[:100],
                "preview": f"[{issue['status']}] {summary}"[:200],
                "discovered_at": scan_started_at,
                "author": issue["creator"]["account_id"] or issue["creator"]["email"],
                "author_name": issue["creator"]["display_name"],
                "group": "Issues",
                "url": f"https://{domain}/browse/{urllib.parse.quote(key, safe='-')}",
                "metadata": {
                    "issue_key": key,
                    "status": issue["status"],
                    "priority": issue["priority"],
                    "issue_type": issue["issue_type"],
                    "updated": updated,
                },
            })

        if len(next_issues) > 500:
            next_issues = dict(
                sorted(
                    next_issues.items(),
                    key=lambda pair: str(pair[1].get("updated", ""))
                    if isinstance(pair[1], dict)
                    else "",
                )[-500:]
            )
        next_state = {
            "version": 4,
            "updated_at": (
                str(state.get("updated_at") or "1970-01-01T00:00:00Z")
                if next_page_token
                else pending_until
            ),
            "initialized": True,
            "scope": scope,
            "issues": next_issues,
            "next_page_token": next_page_token,
            "pending_since_jql": since_jql if next_page_token else "",
            "pending_until": pending_until if next_page_token else "",
        }
        return pollen, self._dump_state(next_state)


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = JiraScanner()
    if data.get("command") == "poll":
        result_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
