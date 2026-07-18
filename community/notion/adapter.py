"""Notion scanner for current database/data-source APIs and watched pages."""

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
from typing import Optional


NOTION_VERSION = "2026-03-11"


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


def _parse_timestamp(value: object) -> datetime | None:
    if not _bounded_text(value, 128, allow_empty=False):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _normalize_user(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    user_id = value.get("id")
    name = value.get("name", "")
    if name is None:
        name = ""
    if (
        not _bounded_text(user_id, 128, allow_empty=False)
        or not _bounded_text(name, 10_000)
    ):
        return None
    return {"id": user_id, "name": name}


def _extract_page_title(page: dict) -> str | None:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return None
    for prop in properties.values():
        if not isinstance(prop, dict):
            return None
        if prop.get("type") != "title":
            continue
        title = prop.get("title")
        if not isinstance(title, list) or len(title) > 10_000:
            return None
        parts: list[str] = []
        for value in title:
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("plain_text"), str)
                or len(value["plain_text"]) > 100_000
            ):
                return None
            parts.append(value["plain_text"])
        return "".join(parts)[:100_000]
    return ""


def _normalize_page(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    page_id = value.get("id")
    edited = value.get("last_edited_time")
    editor = _normalize_user(value.get("last_edited_by"))
    title = _extract_page_title(value)
    url = value.get("url", "")
    if (
        not _bounded_text(page_id, 128, allow_empty=False)
        or _parse_timestamp(edited) is None
        or editor is None
        or title is None
        or not _bounded_text(url, 2_000)
    ):
        return None
    return {
        "id": page_id,
        "edited": edited,
        "editor": editor,
        "title": title,
        "url": url,
    }


def _normalize_comment(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    comment_id = value.get("id")
    created_time = value.get("created_time")
    creator = _normalize_user(value.get("created_by"))
    rich_text = value.get("rich_text")
    if (
        not _bounded_text(comment_id, 128, allow_empty=False)
        or _parse_timestamp(created_time) is None
        or creator is None
        or not isinstance(rich_text, list)
        or len(rich_text) > 10_000
    ):
        return None
    parts: list[str] = []
    for rich_value in rich_text:
        if (
            not isinstance(rich_value, dict)
            or not isinstance(rich_value.get("plain_text"), str)
            or len(rich_value["plain_text"]) > 100_000
        ):
            return None
        parts.append(rich_value["plain_text"])
    return {
        "id": comment_id,
        "created_time": created_time,
        "creator": creator,
        "text": "".join(parts)[:1_000_000],
    }


def _valid_comment_state(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("initialized"), bool):
        return False
    last_created = value.get("last_created_time", "")
    ids = value.get("ids_at_last_time", [])
    missing = value.get("missing_time_ids", [])
    return (
        (last_created == "" or _parse_timestamp(last_created) is not None)
        and isinstance(ids, list)
        and len(ids) <= 1_000
        and all(_bounded_text(item, 128, allow_empty=False) for item in ids)
        and isinstance(missing, list)
        and len(missing) <= 500
        and all(_bounded_text(item, 128, allow_empty=False) for item in missing)
    )


class NotionScanner:
    name = "notion"
    # Five logical targets keep the normal five-minute poll at roughly the
    # manifest's two-requests/minute declaration (pages with comments use two).
    MAX_WATCH_TARGETS = 5
    MAX_RESOLVED_DATA_SOURCES_PER_DATABASE = 5
    _POLL_BUDGET_SECONDS = 45

    def __init__(self):
        self._last_request_at = 0.0

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "NOTION_TOKEN",
            "watch_data_sources": [],
            # Database IDs remain supported by resolving their data_sources.
            "watch_databases": [],
            "watch_pages": [],
            # Comment polling must scan an ascending list; webhooks are better
            # for pages with large comment histories.
            "watch_comments": False,
            "integration_user_id": "",
            "max_items": 100,
            "max_pages": 3,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(
        self,
        path: str,
        token: str,
        method: str = "GET",
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Optional[dict]:
        # Notion documents an average integration limit of three requests per
        # second. Pace locally rather than creating avoidable 429 bursts.
        remaining = 0.35 - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()
        query = urllib.parse.urlencode(params or {})
        url = f"https://api.notion.com/v1{path}" + (f"?{query}" if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        deadline = getattr(self, "_poll_deadline", None)
        timeout = 15.0
        if isinstance(deadline, (int, float)):
            remaining_budget = deadline - time.monotonic()
            if remaining_budget <= 0:
                return None
            timeout = min(timeout, max(0.1, remaining_budget))
        try:
            with _urlopen(req, timeout=timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("response exceeded 2 MB")
                result = _strict_json(raw)
                return result if isinstance(result, dict) else None
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "")
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[notion] HTTP {exc.code}{suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[notion] API error: {exc}", file=sys.stderr)
        return None

    @staticmethod
    def _extract_title(page: dict) -> str:
        return _extract_page_title(page) or ""

    @staticmethod
    def _load_state(watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") in {2, 3, 4}:
            state.setdefault("comment_pages", {})
            state.setdefault("page_canonical_ids", {})
            comments = state["comment_pages"]
            canonical_ids = state["page_canonical_ids"]
            if (
                not isinstance(state.get("initialized"), bool)
                or _parse_timestamp(state.get("updated_at")) is None
                or not isinstance(comments, dict)
                or len(comments) > NotionScanner.MAX_WATCH_TARGETS
                or not all(
                    _bounded_text(page_id, 128, allow_empty=False)
                    and _valid_comment_state(value)
                    for page_id, value in comments.items()
                )
                or not isinstance(canonical_ids, dict)
                or len(canonical_ids) > NotionScanner.MAX_WATCH_TARGETS
                or not all(
                    _bounded_text(configured_id, 128, allow_empty=False)
                    and _bounded_text(canonical_id, 128, allow_empty=False)
                    for configured_id, canonical_id in canonical_ids.items()
                )
            ):
                return None
            if state.get("version") == 4:
                target_times = state.get("target_updated_at")
                target_edits = state.get("target_page_edits")
                if (
                    not isinstance(target_times, dict)
                    or len(target_times) > NotionScanner.MAX_WATCH_TARGETS
                    or not all(
                        _bounded_text(key, 256, allow_empty=False)
                        and _parse_timestamp(value) is not None
                        for key, value in target_times.items()
                    )
                    or not isinstance(target_edits, dict)
                    or len(target_edits) > NotionScanner.MAX_WATCH_TARGETS
                ):
                    return None
                for key, edits in target_edits.items():
                    if (
                        not _bounded_text(key, 256, allow_empty=False)
                        or not isinstance(edits, dict)
                        or len(edits) > 2_000
                        or not all(
                            _bounded_text(page_id, 128, allow_empty=False)
                            and _parse_timestamp(edited) is not None
                            for page_id, edited in edits.items()
                        )
                    ):
                        return None
            return state
        if isinstance(watermark, str) and watermark.lstrip().startswith(("{", "[")):
            return None
        legacy = watermark if isinstance(watermark, str) and watermark else "1970-01-01T00:00:00Z"
        if _parse_timestamp(legacy) is None:
            return None
        return {
            "version": 4,
            "updated_at": legacy,
            "initialized": not legacy.startswith("1970-"),
            "comment_pages": {},
            "target_updated_at": {},
            "target_page_edits": {},
            "page_canonical_ids": {},
        }

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    def _query_data_source(
        self,
        data_source_id: str,
        token: str,
        after: str,
        page_size: int,
        max_pages: int,
    ) -> list[dict] | None:
        pages: list[dict] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for page_number in range(max_pages):
            body: dict[str, object] = {
                "filter": {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"after": after},
                },
                "page_size": page_size,
            }
            if cursor:
                body["start_cursor"] = cursor
            encoded_id = urllib.parse.quote(data_source_id, safe="")
            result = self._api(
                f"/data_sources/{encoded_id}/query", token, method="POST", body=body
            )
            if (
                result is None
                or not isinstance(result.get("results"), list)
                or not isinstance(result.get("has_more"), bool)
            ):
                return None
            page_results = result["results"]
            if not all(isinstance(value, dict) for value in page_results):
                return None
            pages.extend(page_results)
            if not result["has_more"]:
                return pages
            raw_cursor = result.get("next_cursor")
            if (
                not isinstance(raw_cursor, str)
                or not raw_cursor
                or len(raw_cursor) > 2000
                or any(ord(char) < 32 or ord(char) == 127 for char in raw_cursor)
            ):
                return None
            if raw_cursor in seen_cursors:
                return None
            seen_cursors.add(raw_cursor)
            cursor = raw_cursor
            if page_number + 1 >= max_pages:
                print(f"[notion] data source {data_source_id} exceeded max_pages", file=sys.stderr)
                return None
        return pages

    def _list_comments(
        self, page_id: str, token: str, page_size: int, max_pages: int
    ) -> list[dict] | None:
        comments: list[dict] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for page_number in range(max_pages):
            params: dict[str, object] = {"block_id": page_id, "page_size": page_size}
            if cursor:
                params["start_cursor"] = cursor
            result = self._api("/comments", token, params=params)
            if (
                result is None
                or not isinstance(result.get("results"), list)
                or not isinstance(result.get("has_more"), bool)
            ):
                return None
            comment_results = result["results"]
            if not all(isinstance(value, dict) for value in comment_results):
                return None
            comments.extend(comment_results)
            if not result["has_more"]:
                return comments
            raw_cursor = result.get("next_cursor")
            if (
                not isinstance(raw_cursor, str)
                or not raw_cursor
                or len(raw_cursor) > 2000
                or any(ord(char) < 32 or ord(char) == 127 for char in raw_cursor)
            ):
                return None
            if raw_cursor in seen_cursors:
                return None
            seen_cursors.add(raw_cursor)
            cursor = raw_cursor
            if page_number + 1 >= max_pages:
                print(
                    f"[notion] comments for {page_id} exceeded max_pages; use webhooks",
                    file=sys.stderr,
                )
                return None
        return comments

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        token_env = config.get("token_env", "NOTION_TOKEN")
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
        watch_comments = config.get("watch_comments", False)
        if type(watch_comments) is not bool:
            print("[notion] watch_comments must be a boolean", file=sys.stderr)
            return [], watermark
        page_size = config.get("max_items", 100)
        max_pages = config.get("max_pages", 3)
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
            or isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= 5
        ):
            print("[notion] pagination limits are invalid", file=sys.stderr)
            return [], watermark
        configured_sources = config.get("watch_data_sources", [])
        configured_databases = config.get("watch_databases", [])
        configured_pages = config.get("watch_pages", [])
        if (
            not isinstance(configured_sources, list)
            or not isinstance(configured_databases, list)
            or not isinstance(configured_pages, list)
            or len(configured_sources) + len(configured_databases) + len(configured_pages)
            > self.MAX_WATCH_TARGETS
            or not all(
                isinstance(value, str)
                for value in [
                    *configured_sources,
                    *configured_databases,
                    *configured_pages,
                ]
            )
        ):
            print(
                f"[notion] all watch lists may contain at most {self.MAX_WATCH_TARGETS} IDs total",
                file=sys.stderr,
            )
            return [], watermark
        if any(
            not value
            or value != value.strip()
            or len(value) > 128
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            for value in [
                *configured_sources,
                *configured_databases,
                *configured_pages,
            ]
        ):
            print("[notion] watched IDs must contain 1..128 characters", file=sys.stderr)
            return [], watermark
        sources = list(dict.fromkeys(configured_sources))
        databases = list(dict.fromkeys(configured_databases))
        watched_pages = list(dict.fromkeys(configured_pages))

        integration_user_id = config.get("integration_user_id", "")
        if (
            not isinstance(integration_user_id, str)
            or integration_user_id != integration_user_id.strip()
            or len(integration_user_id) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in integration_user_id)
        ):
            return [], watermark

        state = self._load_state(watermark)
        if state is None:
            print("[notion] invalid persisted state; preserving watermark", file=sys.stderr)
            return [], watermark
        initialized = bool(state.get("initialized"))
        legacy_target_mode = initialized and "target_updated_at" not in state
        raw_target_times = state.get("target_updated_at", {})
        if not isinstance(raw_target_times, dict):
            return [], watermark
        active_target_keys = {
            *{f"data_source:{value}" for value in sources},
            *{f"database:{value}" for value in databases},
            *{f"page:{value}" for value in watched_pages},
        }
        target_updated_at = {
            key: value
            for key, value in raw_target_times.items()
            if key in active_target_keys and isinstance(value, str) and value
        }
        raw_target_page_edits = state.get("target_page_edits", {})
        if not isinstance(raw_target_page_edits, dict):
            return [], watermark
        target_page_edits: dict[str, dict[str, str]] = {}
        for target_key, raw_edits in raw_target_page_edits.items():
            if target_key not in active_target_keys:
                continue
            if not isinstance(raw_edits, dict):
                return [], watermark
            bounded_edits = {
                str(page_id): str(edited)
                for page_id, edited in raw_edits.items()
                if isinstance(page_id, str)
                and page_id
                and len(page_id) <= 128
                and isinstance(edited, str)
                and edited
                and len(edited) <= 128
            }
            if len(bounded_edits) != len(raw_edits) or len(bounded_edits) > 2000:
                return [], watermark
            target_page_edits[target_key] = bounded_edits
        legacy_since = str(state.get("updated_at") or "1970-01-01T00:00:00Z")
        if legacy_target_mode:
            target_updated_at.update(
                {key: legacy_since for key in active_target_keys}
            )
        scan_started_at = self._utc_now_z()
        pollen: list[dict] = []
        successful_targets = 0

        def target_window(target_key: str) -> tuple[bool, str] | None:
            baseline = target_updated_at.get(target_key, "")
            if not baseline:
                return False, scan_started_at
            try:
                query_after = (
                    datetime.fromisoformat(baseline.replace("Z", "+00:00"))
                    - timedelta(minutes=5)
                ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (TypeError, ValueError):
                return None
            return True, query_after

        def page_update_pollen(
            page: dict,
            *,
            group: str,
            data_source_id: str = "",
        ) -> dict | None:
            page_id = page["id"]
            edited = page["edited"]
            title = page["title"]
            editor = page["editor"]
            if integration_user_id and editor["id"] == integration_user_id:
                return {}
            edit_hash = hashlib.sha256(edited.encode()).hexdigest()[:10]
            metadata = {
                "page_id": page_id,
                "title": title,
                "last_edited_time": edited,
            }
            if data_source_id:
                metadata["data_source_id"] = data_source_id
            return {
                "id": f"notion-page-{page_id}-{edit_hash}",
                "source": "notion",
                "type": "notion_page_updated",
                "title": title[:100] if title else f"Page {page_id[:8]}",
                "preview": f"Page updated: {title}"[:200],
                "discovered_at": scan_started_at,
                "author": editor["id"],
                "author_name": editor["name"],
                "group": group,
                "url": page["url"],
                "metadata": metadata,
            }

        def query_source_target(
            data_source_id: str,
            query_after: str,
            target_initialized: bool,
            prior_edits: dict[str, str],
        ) -> tuple[list[dict], dict[str, str]] | None:
            source_pages = self._query_data_source(
                data_source_id, token, query_after, page_size, max_pages
            )
            if source_pages is None:
                return None
            target_pollen: list[dict] = []
            candidate_edits = dict(prior_edits)
            seen_page_ids: set[str] = set()
            for raw_page in source_pages:
                page = _normalize_page(raw_page)
                if page is None:
                    return None
                item = page_update_pollen(
                    page,
                    group=f"data-source-{data_source_id[:8]}",
                    data_source_id=data_source_id,
                )
                if item is None:
                    return None
                page_id = page["id"]
                if page_id in seen_page_ids:
                    return None
                seen_page_ids.add(page_id)
                edited = page["edited"]
                if (
                    target_initialized
                    and candidate_edits.get(page_id) != edited
                    and item
                ):
                    target_pollen.append(item)
                candidate_edits[page_id] = edited
            if len(candidate_edits) > 2000:
                candidate_edits = dict(
                    sorted(
                        candidate_edits.items(),
                        key=lambda pair: (_parse_timestamp(pair[1]), pair[0]),
                    )[-2000:]
                )
            return target_pollen, candidate_edits

        for data_source_id in sources:
            target_key = f"data_source:{data_source_id}"
            window = target_window(target_key)
            if window is None:
                return [], watermark
            target_initialized, query_after = window
            source_result = query_source_target(
                data_source_id,
                query_after,
                target_initialized,
                target_page_edits.get(target_key, {}),
            )
            if source_result is None:
                continue
            target_pollen, candidate_edits = source_result
            pollen.extend(target_pollen)
            target_updated_at[target_key] = scan_started_at
            target_page_edits[target_key] = candidate_edits
            successful_targets += 1

        for database_id in databases:
            target_key = f"database:{database_id}"
            window = target_window(target_key)
            if window is None:
                return [], watermark
            target_initialized, query_after = window
            encoded_id = urllib.parse.quote(database_id, safe="")
            database = self._api(f"/databases/{encoded_id}", token)
            raw_data_sources = (
                database.get("data_sources") if isinstance(database, dict) else None
            )
            if (
                not isinstance(raw_data_sources, list)
                or not raw_data_sources
                or len(raw_data_sources)
                > self.MAX_RESOLVED_DATA_SOURCES_PER_DATABASE
            ):
                print(
                    f"[notion] database {database_id} returned an invalid data-source list",
                    file=sys.stderr,
                )
                continue
            resolved_sources: list[str] = []
            malformed_database = False
            for value in raw_data_sources:
                source_id = value.get("id") if isinstance(value, dict) else None
                if (
                    not isinstance(source_id, str)
                    or not source_id
                    or len(source_id) > 128
                    or source_id in resolved_sources
                ):
                    malformed_database = True
                    break
                resolved_sources.append(source_id)
            if malformed_database:
                continue
            target_pollen: list[dict] = []
            candidate_edits = dict(target_page_edits.get(target_key, {}))
            for data_source_id in resolved_sources:
                source_result = query_source_target(
                    data_source_id,
                    query_after,
                    target_initialized,
                    candidate_edits,
                )
                if source_result is None:
                    malformed_database = True
                    break
                source_pollen, candidate_edits = source_result
                target_pollen.extend(source_pollen)
            if malformed_database:
                continue
            pollen.extend(target_pollen)
            target_updated_at[target_key] = scan_started_at
            target_page_edits[target_key] = candidate_edits
            successful_targets += 1

        comment_pages = state.get("comment_pages", {})
        raw_page_canonical_ids = state.get("page_canonical_ids", {})
        if not isinstance(comment_pages, dict) or not isinstance(
            raw_page_canonical_ids, dict
        ):
            return [], watermark
        page_canonical_ids = {
            configured_id: canonical_id
            for configured_id, canonical_id in raw_page_canonical_ids.items()
            if configured_id in watched_pages
            and isinstance(canonical_id, str)
            and canonical_id
        }
        active_comment_page_ids = set(page_canonical_ids.values())
        next_comment_pages = {
            page_id: value
            for page_id, value in comment_pages.items()
            if page_id in active_comment_page_ids and isinstance(value, dict)
        }

        for configured_page_id in watched_pages:
            target_key = f"page:{configured_page_id}"
            window = target_window(target_key)
            if window is None:
                return [], watermark
            target_initialized, query_after = window
            encoded_id = urllib.parse.quote(configured_page_id, safe="")
            raw_page = self._api(f"/pages/{encoded_id}", token)
            if not isinstance(raw_page, dict):
                continue
            page = _normalize_page(raw_page)
            if page is None:
                print("[notion] malformed watched-page response", file=sys.stderr)
                continue
            page_item = page_update_pollen(page, group="pages")
            page_id = page["id"]
            edited = page["edited"]
            previous_canonical_id = page_canonical_ids.get(configured_page_id)
            if previous_canonical_id and previous_canonical_id != page_id:
                next_comment_pages.pop(previous_canonical_id, None)
                active_comment_page_ids.discard(previous_canonical_id)
            page_canonical_ids[configured_page_id] = page_id
            active_comment_page_ids.add(page_id)
            target_updated_at[target_key] = scan_started_at
            prior_page_edits = target_page_edits.get(target_key, {})
            previous_edited = prior_page_edits.get(page_id)
            target_page_edits[target_key] = {page_id: edited}
            successful_targets += 1
            if (
                target_initialized
                and _parse_timestamp(edited) > _parse_timestamp(query_after)
                and previous_edited != edited
                and page_item
            ):
                pollen.append(page_item)

            if not watch_comments:
                continue
            comments = self._list_comments(page_id, token, page_size, max_pages)
            if comments is None:
                continue
            per_page_comments = comment_pages.get(page_id, {})
            if not isinstance(per_page_comments, dict):
                per_page_comments = {}
            comments_initialized = bool(per_page_comments.get("initialized"))
            committed_time = per_page_comments.get("last_created_time", "")
            committed_dt = _parse_timestamp(committed_time) if committed_time else None
            committed_ids = set(per_page_comments.get("ids_at_last_time", []))
            next_time = committed_time
            next_time_dt = committed_dt
            next_ids = set(committed_ids)
            next_missing: set[str] = set()
            comment_pollen: list[dict] = []
            seen_comment_ids: set[str] = set()
            malformed_comments = False
            parsed_comments = [_normalize_comment(comment) for comment in comments]
            if any(comment is None for comment in parsed_comments):
                malformed_comments = True
            for comment in parsed_comments:
                if comment is None:
                    break
                comment_id = comment["id"]
                if comment_id in seen_comment_ids:
                    malformed_comments = True
                    break
                seen_comment_ids.add(comment_id)
                created_time = comment["created_time"]
                created_dt = _parse_timestamp(created_time)
                if next_time_dt is None or created_dt > next_time_dt:
                    next_time = created_time
                    next_time_dt = created_dt
                    next_ids = {comment_id}
                elif created_dt == next_time_dt:
                    next_ids.add(comment_id)
                is_new_comment = (
                    committed_dt is None
                    or created_dt > committed_dt
                    or (created_dt == committed_dt and comment_id not in committed_ids)
                )
                creator = comment["creator"]
                if (
                    not comments_initialized
                    or not is_new_comment
                    or (
                        integration_user_id
                        and creator["id"] == integration_user_id
                    )
                ):
                    continue
                text = comment["text"]
                comment_pollen.append({
                    "id": f"notion-comment-{comment_id}",
                    "source": "notion",
                    "type": "notion_comment",
                    "title": text[:100] or "Comment",
                    "preview": text[:200],
                    "discovered_at": scan_started_at,
                    "author": creator["id"],
                    "author_name": creator["name"],
                    "group": f"page-{page_id[:8]}",
                    "url": page["url"],
                    "metadata": {
                        "page_id": page_id,
                        "comment_id": comment_id,
                        "created_time": created_time,
                    },
                })
            if malformed_comments:
                print(f"[notion] malformed comments for {page_id}", file=sys.stderr)
                continue
            if len(next_missing) > 500 or len(next_ids) > 1000:
                print(
                    f"[notion] comments for {page_id} lack a compact chronological boundary",
                    file=sys.stderr,
                )
                continue
            next_comment_pages[page_id] = {
                "initialized": True,
                "last_created_time": next_time,
                "ids_at_last_time": sorted(next_ids),
                "missing_time_ids": sorted(next_missing),
            }
            pollen.extend(comment_pollen)

        if not watch_comments:
            next_comment_pages = {}
        else:
            next_comment_pages = {
                page_id: value
                for page_id, value in next_comment_pages.items()
                if page_id in active_comment_page_ids
            }
        if successful_targets == 0:
            return [], watermark
        next_state = {
            "version": 4,
            "updated_at": scan_started_at,
            "initialized": True,
            "target_updated_at": target_updated_at,
            "target_page_edits": target_page_edits,
            "comment_pages": next_comment_pages,
            "page_canonical_ids": page_canonical_ids,
        }
        return pollen, self._dump_state(next_state)


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = NotionScanner()
    if data.get("command") == "poll":
        result_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
