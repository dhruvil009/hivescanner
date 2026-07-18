"""Facebook Page Messenger scanner.

The legacy ``/me/notifications`` edge is intentionally not used: reliable Page
notifications should be delivered through Meta Webhooks. This polling adapter
only reads Page conversations that are explicitly configured.
"""

from __future__ import annotations

import json
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
            target_port = -1
            source_port = -2
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


def _valid_conversation_state(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("initialized"), bool)
        and isinstance(value.get("seen_messages"), list)
        and len(value["seen_messages"]) <= 5
        and all(
            _bounded_text(message_id, 128, allow_empty=False)
            for message_id in value["seen_messages"]
        )
    )


def _valid_page_state(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("initialized"), bool):
        return False
    conversations = value.get("conversations")
    next_index = value.get("next_conversation_index")
    if (
        not isinstance(conversations, dict)
        or len(conversations) > FacebookScanner.MAX_TRACKED_CONVERSATIONS
        or not all(
            _bounded_text(conversation_id, 128, allow_empty=False)
            and _valid_conversation_state(state)
            for conversation_id, state in conversations.items()
        )
        or isinstance(next_index, bool)
        or not isinstance(next_index, int)
        or not 0 <= next_index <= 1_000_000
    ):
        return False
    if "legacy_migration" in value and not isinstance(value["legacy_migration"], bool):
        return False
    legacy_seen = value.get("legacy_seen_messages", [])
    return (
        isinstance(legacy_seen, list)
        and len(legacy_seen) <= 500
        and all(
            _bounded_text(message_id, 128, allow_empty=False)
            for message_id in legacy_seen
        )
    )


def _normalize_message(value: object) -> tuple[str, dict] | None:
    if not isinstance(value, dict):
        return None
    message_id = value.get("id")
    text = value.get("message", "")
    sender = value.get("from")
    created_time = value.get("created_time")
    if (
        not _bounded_text(message_id, 128, allow_empty=False)
        or not isinstance(text, str)
        or len(text) > 1_000_000
        or not isinstance(sender, dict)
        or not _bounded_text(sender.get("id"), 128, allow_empty=False)
        or not _bounded_text(sender.get("name", ""), 10_000)
        or not _bounded_text(created_time, 128, allow_empty=False)
    ):
        return None
    return message_id, {
        "message": text,
        "sender_id": sender["id"],
        "sender_name": sender.get("name", ""),
        "created_time": created_time,
    }


class FacebookScanner:
    name = "facebook"
    MAX_TRACKED_CONVERSATIONS = 100
    _POLL_BUDGET_SECONDS = 45

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "FACEBOOK_PAGE_TOKEN",
            "api_version": "v25.0",
            "watch_pages": [],
            "max_items": 100,
            "max_pages": 3,
            "pages_per_poll": 2,
            "conversations_per_page": 4,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _graph(
        self,
        endpoint: str,
        token: str,
        api_version: str,
        params: Optional[dict] = None,
    ) -> Optional[dict]:
        query = urllib.parse.urlencode(params or {})
        url = f"https://graph.facebook.com/{api_version}{endpoint}" + (f"?{query}" if query else "")
        req = urllib.request.Request(
            url,
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
                result = _strict_json(raw)
                if not isinstance(result, dict) or result.get("error"):
                    return None
                return result
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "")
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[facebook] HTTP {exc.code}{suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[facebook] Graph API error: {exc}", file=sys.stderr)
        return None

    @staticmethod
    def _load_state(watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") in {2, 3, 4}:
            pages = state.get("pages")
            if state.get("version") in {2, 3} and isinstance(pages, dict):
                state.setdefault("next_page_index", 0)
                for value in pages.values():
                    if not isinstance(value, dict):
                        continue
                    value.setdefault("initialized", bool(value.get("seen_messages")))
                    value.setdefault("conversations", {})
                    value.setdefault("next_conversation_index", 0)
                    if "seen_messages" in value:
                        value.setdefault("legacy_seen_messages", value["seen_messages"])
            next_index = state.get("next_page_index")
            if (
                not isinstance(state.get("initialized"), bool)
                or not isinstance(pages, dict)
                or len(pages) > 10
                or not all(
                    isinstance(page_id, str)
                    and page_id.isascii()
                    and page_id.isdigit()
                    and 1 <= len(page_id) <= 32
                    and _valid_page_state(value)
                    for page_id, value in pages.items()
                )
                or isinstance(next_index, bool)
                or not isinstance(next_index, int)
                or not 0 <= next_index <= 1_000_000
                or sum(len(value["conversations"]) for value in pages.values())
                > FacebookScanner.MAX_TRACKED_CONVERSATIONS
            ):
                return None
            state["version"] = 4
            return state
        if isinstance(watermark, str) and watermark.lstrip().startswith(("{", "[")):
            return None
        legacy = watermark if isinstance(watermark, str) else ""
        return {
            "version": 4,
            "initialized": bool(legacy and not legacy.startswith("1970-")),
            "pages": {},
            "next_page_index": 0,
        }

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _next_after(result: dict) -> str | None:
        if "paging" not in result:
            return ""
        paging = result.get("paging")
        if not isinstance(paging, dict):
            return None
        next_url = paging.get("next")
        if next_url in (None, ""):
            return ""
        if not _bounded_text(next_url, 10_000, allow_empty=False):
            return None
        cursors = paging.get("cursors")
        if not isinstance(cursors, dict):
            return None
        after = cursors.get("after")
        if (
            not isinstance(after, str)
            or not after
            or len(after) > 2000
            or any(ord(char) < 32 or ord(char) == 127 for char in after)
        ):
            return None
        return after

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        token_env = config.get("token_env", "FACEBOOK_PAGE_TOKEN")
        if not isinstance(token_env, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", token_env
        ):
            print("[facebook] invalid token_env", file=sys.stderr)
            return [], watermark
        token = os.environ.get(token_env, "")
        if (
            not token
            or len(token) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in token)
        ):
            return [], watermark
        api_version = config.get("api_version", "v25.0")
        if not isinstance(api_version, str) or not re.fullmatch(
            r"v\d{1,3}\.\d{1,2}", api_version
        ):
            print("[facebook] invalid api_version", file=sys.stderr)
            return [], watermark
        raw_pages = config.get("watch_pages", [])
        if (
            not isinstance(raw_pages, list)
            or len(raw_pages) > 10
            or not all(isinstance(value, str) for value in raw_pages)
        ):
            print("[facebook] watch_pages must contain at most 10 page IDs", file=sys.stderr)
            return [], watermark
        pages = list(dict.fromkeys(raw_pages))
        if not all(
            value.isascii() and value.isdigit() and 1 <= len(value) <= 32
            for value in pages
        ):
            print("[facebook] watch_pages entries must be numeric Page IDs", file=sys.stderr)
            return [], watermark
        if not pages:
            return [], watermark
        page_size = config.get("max_items", 100)
        max_pages = config.get("max_pages", 3)
        pages_per_poll = config.get("pages_per_poll", 2)
        conversations_per_page = config.get("conversations_per_page", 4)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (
                    page_size,
                    max_pages,
                    pages_per_poll,
                    conversations_per_page,
                )
            )
            or not 1 <= page_size <= 100
            or not 1 <= max_pages <= 5
            or not 1 <= pages_per_poll <= 10
            or not 1 <= conversations_per_page <= 20
        ):
            print("[facebook] pagination limits are invalid", file=sys.stderr)
            return [], watermark

        state = self._load_state(watermark)
        if state is None:
            print("[facebook] invalid persisted state; preserving watermark", file=sys.stderr)
            return [], watermark
        page_states = state.get("pages", {})
        if not isinstance(page_states, dict):
            return [], watermark
        next_page_states: dict[str, dict] = {
            page_id: value
            for page_id, value in page_states.items()
            if page_id in pages and isinstance(value, dict)
        }
        pollen: list[dict] = []
        discovered_at = self._utc_now_z()
        next_page_index = state.get("next_page_index", 0) % len(pages)
        initial_page_index = next_page_index
        selected_pages = [
            pages[(next_page_index + offset) % len(pages)]
            for offset in range(min(pages_per_poll, len(pages)))
        ]
        made_progress = len(next_page_states) != len(page_states)

        for page_id in selected_pages:
            per_page = page_states.get(page_id, {})
            if not isinstance(per_page, dict):
                per_page = {}
            page_initialized = bool(per_page.get("initialized")) or bool(
                per_page.get("seen_messages")
            )
            legacy_seen = {
                str(value)
                for value in (
                    per_page.get("legacy_seen_messages")
                    or per_page.get("seen_messages", [])
                )
                if value
            } if isinstance(
                per_page.get("legacy_seen_messages")
                or per_page.get("seen_messages", []),
                list,
            ) else set()
            conversation_states = (
                per_page.get("conversations")
                if isinstance(per_page.get("conversations"), dict)
                else {}
            )
            legacy_migration = bool(
                per_page.get("legacy_migration")
                or (legacy_seen and not conversation_states)
            )
            conversations: list[dict] = []
            conversation_ids: set[str] = set()
            cursor = ""
            seen_cursors: set[str] = set()
            encoded_page = urllib.parse.quote(page_id, safe="")
            page_error = False
            for page_number in range(max_pages):
                params = {
                    "fields": "id,updated_time,participants",
                    "limit": page_size,
                    "platform": "messenger",
                }
                if cursor:
                    params["after"] = cursor
                result = self._graph(
                    f"/{encoded_page}/conversations", token, api_version, params
                )
                if result is None or not isinstance(result.get("data"), list):
                    page_error = True
                    break
                page_conversations = result["data"]
                if not all(isinstance(value, dict) for value in page_conversations):
                    page_error = True
                    break
                for conversation in page_conversations:
                    conversation_id = conversation.get("id")
                    if (
                        not isinstance(conversation_id, str)
                        or not conversation_id
                        or len(conversation_id) > 128
                        or conversation_id in conversation_ids
                    ):
                        page_error = True
                        break
                    conversation_ids.add(conversation_id)
                    conversations.append(conversation)
                if page_error:
                    break
                if len(conversations) > self.MAX_TRACKED_CONVERSATIONS:
                    print(
                        f"[facebook] Page {page_id} has too many active conversations; use webhooks",
                        file=sys.stderr,
                    )
                    page_error = True
                    break
                next_cursor = self._next_after(result)
                if next_cursor is None:
                    page_error = True
                    break
                if next_cursor and next_cursor in seen_cursors:
                    page_error = True
                    break
                if next_cursor:
                    seen_cursors.add(next_cursor)
                cursor = next_cursor
                if not cursor:
                    break
                if page_number + 1 >= max_pages:
                    print(f"[facebook] Page {page_id} conversations exceeded max_pages", file=sys.stderr)
                    page_error = True
            if page_error:
                continue

            active_conversation_ids = sorted(conversation_ids)
            next_conversation_states: dict[str, dict] = {
                conversation_id: value
                for conversation_id, value in conversation_states.items()
                if conversation_id in conversation_ids and isinstance(value, dict)
            }
            if not page_initialized or legacy_migration:
                for conversation_id in active_conversation_ids:
                    next_conversation_states.setdefault(
                        conversation_id,
                        {"initialized": False, "seen_messages": []},
                    )
            try:
                conversation_index = (
                    int(per_page.get("next_conversation_index", 0) or 0)
                    % len(active_conversation_ids)
                    if active_conversation_ids
                    else 0
                )
            except (TypeError, ValueError):
                conversation_index = 0
            selected_conversations = [
                active_conversation_ids[
                    (conversation_index + offset) % len(active_conversation_ids)
                ]
                for offset in range(
                    min(conversations_per_page, len(active_conversation_ids))
                )
            ]

            for conversation_id in selected_conversations:
                had_prior_conversation = conversation_id in conversation_states
                prior_conversation = conversation_states.get(conversation_id, {})
                if not isinstance(prior_conversation, dict):
                    prior_conversation = {}
                seen = {
                    str(value)
                    for value in prior_conversation.get("seen_messages", [])
                } if isinstance(prior_conversation.get("seen_messages", []), list) else set()
                conversation_initialized = bool(
                    prior_conversation.get("initialized")
                ) and not legacy_migration
                is_new_conversation = (
                    page_initialized
                    and not had_prior_conversation
                    and not legacy_migration
                )
                emit_messages = conversation_initialized or is_new_conversation
                newly_seen: list[str] = []
                conversation_pollen: list[dict] = []
                cursor = ""
                seen_cursors = set()
                fetched_message_ids: set[str] = set()
                reached_known = False
                conversation_error = False
                encoded_conversation = urllib.parse.quote(conversation_id, safe="")
                pages_to_fetch = (
                    max_pages
                    if conversation_initialized or is_new_conversation
                    else 1
                )
                for page_number in range(pages_to_fetch):
                    params = {
                        "fields": "id,message,from,to,created_time",
                        "limit": page_size,
                    }
                    if cursor:
                        params["after"] = cursor
                    result = self._graph(
                        f"/{encoded_conversation}/messages", token, api_version, params
                    )
                    if result is None or not isinstance(result.get("data"), list):
                        conversation_error = True
                        break
                    page_messages = result["data"]
                    if not all(isinstance(message, dict) for message in page_messages):
                        conversation_error = True
                        break
                    for message in page_messages:
                        normalized = _normalize_message(message)
                        if (
                            normalized is None
                            or normalized[0] in fetched_message_ids
                        ):
                            conversation_error = True
                            break
                        message_id, parsed_message = normalized
                        fetched_message_ids.add(message_id)
                        if message_id in seen:
                            reached_known = True
                            # Message pages are newest-first; everything after
                            # the first known ID is older than the committed
                            # boundary and must not be rediscovered after state
                            # compaction.
                            break
                        if message_id not in newly_seen:
                            newly_seen.append(message_id)
                        if (
                            not emit_messages
                            or parsed_message["sender_id"] == page_id
                        ):
                            continue
                        text = parsed_message["message"]
                        conversation_pollen.append({
                            "id": f"facebook-msg-{message_id}",
                            "source": "facebook",
                            "type": "facebook_message",
                            "title": f"Message from {parsed_message['sender_name'] or 'Unknown'}",
                            "preview": text[:200],
                            "discovered_at": discovered_at,
                            "author": parsed_message["sender_id"],
                            "author_name": parsed_message["sender_name"],
                            "group": f"Page {page_id}",
                            "url": "",
                            "metadata": {
                                "page_id": page_id,
                                "conversation_id": conversation_id,
                                "message_id": message_id,
                                "created_time": parsed_message["created_time"],
                            },
                        })
                    if conversation_error:
                        break
                    if reached_known:
                        break
                    next_cursor = self._next_after(result)
                    if next_cursor is None:
                        conversation_error = True
                        break
                    if next_cursor and next_cursor in seen_cursors:
                        conversation_error = True
                        break
                    if next_cursor:
                        seen_cursors.add(next_cursor)
                    cursor = next_cursor
                    if not cursor:
                        break
                    if page_number + 1 >= pages_to_fetch:
                        if emit_messages:
                            print(f"[facebook] conversation {conversation_id} exceeded max_pages", file=sys.stderr)
                            conversation_error = True
                        break
                if conversation_error:
                    continue
                retained = list(dict.fromkeys([
                    *newly_seen,
                    *(
                        prior_conversation.get("seen_messages", [])
                        if isinstance(prior_conversation.get("seen_messages", []), list)
                        else []
                    ),
                ]))[:5]
                next_conversation_states[conversation_id] = {
                    "initialized": True,
                    "seen_messages": retained,
                }
                pollen.extend(conversation_pollen)

            if active_conversation_ids:
                conversation_index = (
                    conversation_index + len(selected_conversations)
                ) % len(active_conversation_ids)
            page_state = {
                "initialized": True,
                "conversations": next_conversation_states,
                "next_conversation_index": conversation_index,
            }
            if legacy_migration and any(
                not bool(value.get("initialized"))
                for value in next_conversation_states.values()
                if isinstance(value, dict)
            ):
                page_state["legacy_migration"] = True
                page_state["legacy_seen_messages"] = sorted(legacy_seen)[-500:]
            next_page_states[page_id] = page_state
            made_progress = True

        next_page_index = (next_page_index + len(selected_pages)) % len(pages)
        tracked_conversations = sum(
            len(value.get("conversations", {}))
            for value in next_page_states.values()
            if isinstance(value, dict)
            and isinstance(value.get("conversations", {}), dict)
        )
        if tracked_conversations > self.MAX_TRACKED_CONVERSATIONS:
            print(
                f"[facebook] More than {self.MAX_TRACKED_CONVERSATIONS} active conversations; use webhooks",
                file=sys.stderr,
            )
            return [], watermark
        if not made_progress and next_page_index == initial_page_index:
            return [], watermark
        next_state = {
            "version": 4,
            "initialized": True,
            "pages": next_page_states,
            "next_page_index": next_page_index,
        }
        return pollen, self._dump_state(next_state)


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = FacebookScanner()
    if data.get("command") == "poll":
        poll_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": poll_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
