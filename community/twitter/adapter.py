"""X scanner with transactional mention and direct-message watermarks."""

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
            or (source.hostname or "").casefold()
            != (target.hostname or "").casefold()
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


def _strict_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_json_loads(value: str | bytes) -> object:
    return json.loads(
        value,
        object_pairs_hook=_strict_object,
        parse_constant=_reject_constant,
    )


class TwitterScanner:
    name = "twitter"
    _POLL_BUDGET_SECONDS = 45
    _USERNAME_CACHE_SECONDS = 24 * 60 * 60
    _MAX_STATE_IDS = 5000

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "TWITTER_BEARER_TOKEN",
            "dm_token_env": "TWITTER_USER_TOKEN",
            "username": "",
            "user_id": "",
            "watch_mentions": True,
            "watch_dms": False,
            "max_items": 100,
            "max_pages": 10,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _valid_env_name(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return value if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) else ""

    @staticmethod
    def _credential(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 8192:
            return ""
        return value if all(ord(char) >= 32 and ord(char) != 127 for char in value) else ""

    @staticmethod
    def _snowflake(value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value):
            return ""
        return value if int(value) > 0 else ""

    @staticmethod
    def _username(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return value if re.fullmatch(r"[A-Za-z0-9_]{1,15}", value) else ""

    @staticmethod
    def _cursor(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 1024:
            return ""
        return value if all(ord(char) >= 32 and ord(char) != 127 for char in value) else ""

    @staticmethod
    def _timestamp(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            return ""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
        return value if parsed.tzinfo is not None else ""

    @staticmethod
    def _bounded_text(value: object, *, limit: int, optional: bool = False) -> str | None:
        if value is None and optional:
            return ""
        if not isinstance(value, str) or len(value) > limit:
            return None
        return value

    @staticmethod
    def _default_state() -> dict:
        return {
            "version": 5,
            "resolved_username": "",
            "resolved_user_id": "",
            "resolved_at": 0,
            "mentions_initialized": False,
            "mention_user_id": "",
            "mention_since_id": "",
            "mention_cursor": "",
            "mention_pending_highest": "",
            "dms_initialized": False,
            "dm_user_id": "",
            "seen_dms": [],
            "dm_cursor": "",
            "dm_pending_ids": [],
        }

    def _api(self, path: str, params: dict, token: str) -> dict | None:
        deadline = getattr(self, "_poll_deadline", None)
        timeout = 15.0
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            timeout = min(timeout, max(0.1, remaining))
        try:
            query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
            url = f"https://api.x.com/2/{path}" + (f"?{query}" if query else "")
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "HiveScanner/1.0",
                },
            )
            with _urlopen(req, timeout=timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("response exceeded 2 MB")
                result = _strict_json_loads(raw)
                return result if isinstance(result, dict) else None
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[twitter] HTTP {exc.code}{suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"[twitter] API error: {exc}", file=sys.stderr)
        return None

    @classmethod
    def _load_state(cls, watermark: str) -> dict | None:
        if watermark in {"", "1970-01-01T00:00:00Z"}:
            return cls._default_state()
        try:
            state = _strict_json_loads(watermark)
            if not isinstance(state, dict) or state.get("version") not in {3, 4, 5}:
                return None

            def optional_id(field: str) -> str:
                value = state.get(field, "")
                if value == "":
                    return ""
                clean = cls._snowflake(value)
                if not clean:
                    raise ValueError(f"invalid {field}")
                return clean

            def boolean(field: str) -> bool:
                value = state.get(field, False)
                if type(value) is not bool:
                    raise ValueError(f"invalid {field}")
                return value

            def cursor(field: str) -> str:
                value = state.get(field, "")
                if value == "":
                    return ""
                clean = cls._cursor(value)
                if not clean:
                    raise ValueError(f"invalid {field}")
                return clean

            def ids(field: str) -> list[str]:
                values = state.get(field, [])
                if not isinstance(values, list) or len(values) > cls._MAX_STATE_IDS:
                    raise ValueError(f"invalid {field}")
                clean = []
                for value in values:
                    item = cls._snowflake(value)
                    if not item:
                        raise ValueError(f"invalid {field}")
                    clean.append(item)
                if len(set(clean)) != len(clean):
                    raise ValueError(f"duplicate IDs in {field}")
                return clean

            resolved_username = state.get("resolved_username", "")
            if resolved_username != "" and not cls._username(resolved_username):
                raise ValueError("invalid resolved_username")
            resolved_user_id = optional_id("resolved_user_id")
            resolved_at = state.get("resolved_at", 0)
            if (
                isinstance(resolved_at, bool)
                or not isinstance(resolved_at, int)
                or not 0 <= resolved_at <= 2**63 - 1
            ):
                raise ValueError("invalid resolved_at")
            if bool(resolved_username) != bool(resolved_user_id):
                raise ValueError("incomplete username resolution cache")
            if not resolved_username and resolved_at != 0:
                raise ValueError("orphaned username resolution timestamp")

            loaded = {
                "version": 5,
                "resolved_username": resolved_username,
                "resolved_user_id": resolved_user_id,
                "resolved_at": resolved_at,
                "mentions_initialized": boolean("mentions_initialized"),
                "mention_user_id": optional_id("mention_user_id"),
                "mention_since_id": optional_id("mention_since_id"),
                "mention_cursor": cursor("mention_cursor"),
                "mention_pending_highest": optional_id(
                    "mention_pending_highest"
                ),
                "dms_initialized": boolean("dms_initialized"),
                "dm_user_id": optional_id("dm_user_id"),
                "seen_dms": ids("seen_dms"),
                "dm_cursor": cursor("dm_cursor"),
                "dm_pending_ids": ids("dm_pending_ids"),
            }
            if loaded["mention_cursor"] and not loaded["mentions_initialized"]:
                raise ValueError("mention cursor without initialized stream")
            if loaded["dm_cursor"] and not loaded["dms_initialized"]:
                raise ValueError("DM cursor without initialized stream")
            return loaded
        except (TypeError, UnicodeError, ValueError):
            return None

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _response_page(cls, response: object) -> tuple[list, str] | None:
        if not isinstance(response, dict):
            return None
        errors = response.get("errors")
        if errors is not None and (
            not isinstance(errors, list)
            or not all(isinstance(item, dict) for item in errors)
        ):
            return None
        if errors and "data" not in response:
            return None
        data = response.get("data", [])
        if not isinstance(data, list):
            return None
        meta = response.get("meta", {})
        if not isinstance(meta, dict):
            return None
        result_count = meta.get("result_count")
        if result_count is not None and (
            isinstance(result_count, bool)
            or not isinstance(result_count, int)
            or result_count != len(data)
        ):
            return None
        if "next_token" not in meta:
            return data, ""
        next_token = cls._cursor(meta.get("next_token"))
        if not next_token:
            return None
        return data, next_token

    @classmethod
    def _resolve_username(cls, response: object, expected: str) -> str:
        if not isinstance(response, dict):
            return ""
        errors = response.get("errors")
        if errors is not None and (
            not isinstance(errors, list)
            or not all(isinstance(item, dict) for item in errors)
        ):
            return ""
        data = response.get("data")
        if errors and not isinstance(data, dict):
            return ""
        if not isinstance(data, dict):
            return ""
        user_id = cls._snowflake(data.get("id"))
        username = cls._username(data.get("username"))
        if not user_id or not username or username.casefold() != expected.casefold():
            return ""
        return user_id

    @staticmethod
    def _config_int(config: dict, field: str, default: int, low: int, high: int) -> int | None:
        value = config.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            return None
        return value

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        if not isinstance(config, dict) or not isinstance(watermark, str):
            return [], watermark if isinstance(watermark, str) else ""

        watch_mentions = config.get("watch_mentions", True)
        watch_dms = config.get("watch_dms", False)
        if type(watch_mentions) is not bool or type(watch_dms) is not bool:
            print("[twitter] watch flags must be booleans", file=sys.stderr)
            return [], watermark
        max_items = self._config_int(config, "max_items", 100, 5, 100)
        max_pages = self._config_int(config, "max_pages", 10, 1, 10)
        if max_items is None or max_pages is None:
            print("[twitter] pagination limits are invalid", file=sys.stderr)
            return [], watermark
        if not watch_mentions and not watch_dms:
            return [], watermark

        mention_env = config.get("token_env", "TWITTER_BEARER_TOKEN")
        dm_env = config.get("dm_token_env", "TWITTER_USER_TOKEN")
        if watch_mentions and not self._valid_env_name(mention_env):
            print("[twitter] mention token environment name is invalid", file=sys.stderr)
            return [], watermark
        if watch_dms and not self._valid_env_name(dm_env):
            print("[twitter] DM token environment name is invalid", file=sys.stderr)
            return [], watermark

        state = self._load_state(watermark)
        if state is None:
            print("[twitter] watermark is invalid; preserving it", file=sys.stderr)
            return [], watermark

        mention_token = (
            self._credential(os.environ.get(mention_env, ""))
            if watch_mentions
            else ""
        )
        dm_token = (
            self._credential(os.environ.get(dm_env, "")) if watch_dms else ""
        )

        raw_user_id = config.get("user_id", "")
        if not isinstance(raw_user_id, str):
            print("[twitter] user_id must be a string", file=sys.stderr)
            return [], watermark
        raw_user_id = raw_user_id.strip()
        user_id = self._snowflake(raw_user_id) if raw_user_id else ""
        if raw_user_id and not user_id:
            print("[twitter] user_id must be a positive numeric X user ID", file=sys.stderr)
            return [], watermark

        raw_username = config.get("username", "")
        if not isinstance(raw_username, str):
            print("[twitter] username must be a string", file=sys.stderr)
            return [], watermark
        raw_username = raw_username.strip().removeprefix("@")
        username = self._username(raw_username) if raw_username else ""
        if raw_username and not username:
            print("[twitter] username is invalid", file=sys.stderr)
            return [], watermark

        resolution_changed = False
        now_epoch = int(time.time())
        if not user_id:
            if not username:
                print("[twitter] user_id or username is required", file=sys.stderr)
                return [], watermark
            cache_age = now_epoch - state["resolved_at"]
            cache_valid = (
                state["resolved_username"].casefold() == username.casefold()
                and bool(state["resolved_user_id"])
                and 0 <= cache_age < self._USERNAME_CACHE_SECONDS
            )
            if cache_valid:
                user_id = state["resolved_user_id"]
            else:
                lookup_token = mention_token or dm_token
                if not lookup_token:
                    print(
                        "[twitter] resolving username requires an available token",
                        file=sys.stderr,
                    )
                    return [], watermark
                encoded_username = urllib.parse.quote(username, safe="")
                response = self._api(
                    f"users/by/username/{encoded_username}",
                    {"user.fields": "username"},
                    lookup_token,
                )
                user_id = self._resolve_username(response, username)
                if not user_id:
                    print("[twitter] could not resolve username", file=sys.stderr)
                    return [], watermark
                state["resolved_username"] = username
                state["resolved_user_id"] = user_id
                state["resolved_at"] = now_epoch
                resolution_changed = True

        pollen: list[dict] = []
        discovered_at = self._utc_now_z()

        mention_same_user = state["mention_user_id"] == user_id
        mentions_initialized = state["mentions_initialized"] and mention_same_user
        committed_mention_id = state["mention_since_id"] if mention_same_user else ""
        highest_mention_id = (
            state["mention_pending_highest"] or committed_mention_id
            if mentions_initialized
            else committed_mention_id
        )
        original_mention_cursor = (
            state["mention_cursor"] if mentions_initialized else ""
        )
        pagination_token = original_mention_cursor
        mention_progress = False
        mention_window_complete = False
        seen_mentions: set[str] = set()

        if watch_mentions and mention_token:
            for _page in range(1 if not mentions_initialized else max_pages):
                params = {
                    "max_results": max_items,
                    "tweet.fields": "created_at,author_id,text",
                    "expansions": "author_id",
                    "user.fields": "username,name",
                }
                if mentions_initialized and committed_mention_id:
                    params["since_id"] = committed_mention_id
                if pagination_token:
                    params["pagination_token"] = pagination_token
                response = self._api(
                    f"users/{urllib.parse.quote(user_id, safe='')}/mentions",
                    params,
                    mention_token,
                )
                page = self._response_page(response)
                if page is None:
                    break
                data, next_token = page

                includes = response.get("includes", {})
                if not isinstance(includes, dict):
                    break
                users = includes.get("users", [])
                if not isinstance(users, list):
                    break
                user_lookup = {}
                valid_page = True
                for value in users:
                    if not isinstance(value, dict):
                        valid_page = False
                        break
                    author_id = self._snowflake(value.get("id"))
                    author_username = value.get("username", "")
                    author_name = value.get("name", "")
                    if (
                        not author_id
                        or author_id in user_lookup
                        or not isinstance(author_username, str)
                        or (author_username and not self._username(author_username))
                        or not isinstance(author_name, str)
                        or len(author_name) > 200
                    ):
                        valid_page = False
                        break
                    user_lookup[author_id] = {
                        "username": author_username,
                        "name": author_name,
                    }
                if not valid_page:
                    break

                page_items = []
                page_highest = highest_mention_id
                page_ids: set[str] = set()
                for tweet in data:
                    if not isinstance(tweet, dict):
                        valid_page = False
                        break
                    tweet_id = self._snowflake(tweet.get("id"))
                    author_id = self._snowflake(tweet.get("author_id"))
                    tweet_text = self._bounded_text(tweet.get("text"), limit=50_000)
                    created_at = self._timestamp(tweet.get("created_at"))
                    if (
                        not tweet_id
                        or not author_id
                        or tweet_text is None
                        or not created_at
                        or tweet_id in page_ids
                    ):
                        valid_page = False
                        break
                    page_ids.add(tweet_id)
                    if not page_highest or int(tweet_id) > int(page_highest):
                        page_highest = tweet_id
                    if (
                        not mentions_initialized
                        or author_id == user_id
                        or tweet_id in seen_mentions
                        or (
                            committed_mention_id
                            and int(tweet_id) <= int(committed_mention_id)
                        )
                    ):
                        continue
                    author = user_lookup.get(author_id, {})
                    author_username = author.get("username", "")
                    page_items.append(
                        {
                            "id": f"twitter-mention-{tweet_id}",
                            "source": "twitter",
                            "type": "twitter_mention",
                            "title": tweet_text[:100] or "Mention without text",
                            "preview": tweet_text[:200],
                            "discovered_at": discovered_at,
                            "author": author_username or author_id,
                            "author_name": author.get("name", ""),
                            "group": "Mentions",
                            "url": (
                                f"https://x.com/{author_username}/status/{tweet_id}"
                                if author_username
                                else f"https://x.com/i/status/{tweet_id}"
                            ),
                            "metadata": {
                                "tweet_id": tweet_id,
                                "author_id": author_id,
                                "created_at": created_at,
                            },
                        }
                    )
                if not valid_page:
                    break

                seen_mentions.update(page_ids)
                pollen.extend(page_items)
                highest_mention_id = page_highest
                pagination_token = next_token
                mention_progress = True
                if not mentions_initialized or not pagination_token:
                    mention_window_complete = True
                    pagination_token = ""
                    break
        elif watch_mentions:
            print("[twitter] watch_mentions requires a bearer token", file=sys.stderr)

        dm_same_user = state["dm_user_id"] == user_id
        seen_dms = set(state["seen_dms"]) if dm_same_user else set()
        dms_initialized = state["dms_initialized"] and dm_same_user
        original_dm_cursor = state["dm_cursor"] if dms_initialized else ""
        dm_pagination_token = original_dm_cursor
        pending_dms = (
            set(state["dm_pending_ids"])
            if dms_initialized and original_dm_cursor
            else set()
        )
        next_pending_dms = set(pending_dms)
        next_seen_dms = set(seen_dms)
        dm_progress = False
        dm_completed = False

        if watch_dms and dm_token:
            params = {
                "dm_event.fields": (
                    "created_at,sender_id,text,dm_conversation_id,event_type"
                ),
                "event_types": "MessageCreate",
                "max_results": min(max_items, 100),
            }
            if dm_pagination_token:
                params["pagination_token"] = dm_pagination_token
            response = self._api("dm_events", params, dm_token)
            page = self._response_page(response)
            if page is not None:
                events, next_token = page
                normalized_events = []
                previous_id: int | None = None
                valid_page = True
                for event in events:
                    if not isinstance(event, dict):
                        valid_page = False
                        break
                    event_id = self._snowflake(event.get("id"))
                    sender_id = self._snowflake(event.get("sender_id"))
                    event_type = event.get("event_type")
                    text = self._bounded_text(
                        event.get("text"), limit=50_000, optional=True
                    )
                    conversation_id = self._bounded_text(
                        event.get("dm_conversation_id"), limit=200
                    )
                    created_at = self._timestamp(event.get("created_at"))
                    if (
                        not event_id
                        or not sender_id
                        or event_type != "MessageCreate"
                        or text is None
                        or not conversation_id
                        or not created_at
                    ):
                        valid_page = False
                        break
                    numeric_id = int(event_id)
                    if previous_id is not None and numeric_id >= previous_id:
                        valid_page = False
                        break
                    previous_id = numeric_id
                    normalized_events.append(
                        (
                            event_id,
                            sender_id,
                            text,
                            conversation_id,
                            created_at,
                        )
                    )

                if valid_page:
                    reached_known = False
                    page_items = []
                    for (
                        event_id,
                        sender_id,
                        text,
                        conversation_id,
                        created_at,
                    ) in normalized_events:
                        if event_id in seen_dms:
                            reached_known = True
                            break
                        if event_id in next_pending_dms:
                            continue
                        next_pending_dms.add(event_id)
                        if not dms_initialized or sender_id == user_id:
                            continue
                        title = text or "Direct message with non-text content"
                        page_items.append(
                            {
                                "id": f"twitter-dm-{event_id}",
                                "source": "twitter",
                                "type": "twitter_dm",
                                "title": title[:100],
                                "preview": text[:200],
                                "discovered_at": discovered_at,
                                "author": sender_id,
                                "author_name": "",
                                "group": "DMs",
                                "url": "https://x.com/messages",
                                "metadata": {
                                    "event_id": event_id,
                                    "sender_id": sender_id,
                                    "conversation_id": conversation_id,
                                    "created_at": created_at,
                                },
                            }
                        )
                    pollen.extend(page_items)
                    dm_progress = True
                    dm_pagination_token = next_token
                    if not dms_initialized or reached_known or not next_token:
                        dm_completed = True
                        dm_pagination_token = ""
                        next_seen_dms.update(next_pending_dms)
        elif watch_dms:
            print(
                "[twitter] watch_dms requires a user-context token",
                file=sys.stderr,
            )

        next_state = dict(state)
        next_state["version"] = 5
        if mention_progress:
            next_state["mentions_initialized"] = True
            next_state["mention_user_id"] = user_id
            if not mentions_initialized or mention_window_complete:
                next_state["mention_since_id"] = highest_mention_id
                next_state["mention_cursor"] = ""
                next_state["mention_pending_highest"] = ""
            else:
                next_state["mention_since_id"] = committed_mention_id
                next_state["mention_cursor"] = pagination_token
                next_state["mention_pending_highest"] = highest_mention_id

        if dm_progress:
            next_state["dms_initialized"] = True
            next_state["dm_user_id"] = user_id
            if not dms_initialized or dm_completed:
                next_state["seen_dms"] = sorted(
                    next_seen_dms,
                    key=int,
                )[-self._MAX_STATE_IDS :]
                next_state["dm_cursor"] = ""
                next_state["dm_pending_ids"] = []
            else:
                next_state["seen_dms"] = sorted(seen_dms, key=int)[
                    -self._MAX_STATE_IDS :
                ]
                next_state["dm_cursor"] = dm_pagination_token
                next_state["dm_pending_ids"] = sorted(
                    next_pending_dms,
                    key=int,
                )[-self._MAX_STATE_IDS :]

        if not (resolution_changed or mention_progress or dm_progress):
            return [], watermark
        return pollen, self._dump_state(next_state)


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json_loads(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = TwitterScanner()
    if data.get("command") == "poll":
        poll_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": poll_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
