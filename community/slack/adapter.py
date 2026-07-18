"""Slack scanner with per-channel cursors and conservative rate limiting."""

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
from decimal import Decimal, InvalidOperation
from typing import Optional


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


def _slack_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]{1,32}", value) is not None


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


class SlackScanner:
    name = "slack"
    _POLL_BUDGET_SECONDS = 45

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "SLACK_TOKEN",
            "watch_channels": [],
            "watch_dms": True,
            "user_id": "",
            "max_messages": 15,
            # New non-Marketplace apps can be limited to one history request/min.
            "history_requests_per_poll": 1,
            "allow_high_tier_rate_limits": False,
            "dm_discovery_max_pages": 10,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(
        self, method: str, token: str, params: Optional[dict] = None
    ) -> Optional[dict]:
        query = urllib.parse.urlencode(params or {})
        url = f"https://slack.com/api/{method}" + (f"?{query}" if query else "")
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
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("response exceeded 1 MB")
                data = _strict_json(raw)
                return data if isinstance(data, dict) else None
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "")
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[slack] HTTP {exc.code} ({method}){suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[slack] API error ({method}): {exc}", file=sys.stderr)
        return None

    @staticmethod
    def _cursor(value: object) -> str:
        if not isinstance(value, str) or len(value) > 1024:
            return ""
        return value if all(ord(char) >= 32 and ord(char) != 127 for char in value) else ""

    @classmethod
    def _load_state(cls, watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") == 2:
            required_keys = {
                "version",
                "channels",
                "next_index",
                "dm_channels",
                "dm_refreshed_at",
            }
            allowed_keys = required_keys | {"authenticated_user_id", "legacy_oldest"}
            channels = state.get("channels")
            dm_channels = state.get("dm_channels")
            next_index = state.get("next_index")
            refreshed = state.get("dm_refreshed_at")
            authenticated = state.get("authenticated_user_id", "")
            legacy_oldest = state.get("legacy_oldest", "0")
            if (
                not required_keys.issubset(state)
                or not set(state).issubset(allowed_keys)
                or type(state.get("version")) is not int
                or not isinstance(channels, dict)
                or len(channels) > 500
                or not isinstance(dm_channels, list)
                or len(dm_channels) > 200
                or isinstance(next_index, bool)
                or not isinstance(next_index, int)
                or not 0 <= next_index <= 1_000_000
                or not isinstance(refreshed, str)
                or len(refreshed) > 64
                or (authenticated != "" and not _slack_id(authenticated))
                or not isinstance(legacy_oldest, str)
                or cls._timestamp(legacy_oldest) is None
            ):
                return None
            if refreshed:
                try:
                    parsed = datetime.fromisoformat(refreshed.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        return None
                except ValueError:
                    return None
            clean_dms = []
            for channel_id in dm_channels:
                if not _slack_id(channel_id):
                    return None
                clean_dms.append(channel_id)
            if len(set(clean_dms)) != len(clean_dms):
                return None
            clean_channels = {}
            for channel_id, value in channels.items():
                if not _slack_id(channel_id) or not isinstance(value, dict):
                    return None
                initialized = value.get("initialized")
                oldest = value.get("oldest")
                pending = value.get("pending_highest", oldest)
                cursor = value.get("cursor", "")
                value_keys = set(value)
                cursor_keys = {"initialized", "oldest", "cursor", "pending_highest"}
                settled_keys = {"initialized", "oldest"}
                oldest_value = cls._timestamp(oldest)
                pending_value = cls._timestamp(pending)
                if (
                    type(initialized) is not bool
                    or not isinstance(oldest, str)
                    or oldest_value is None
                    or not isinstance(pending, str)
                    or pending_value is None
                    or pending_value < oldest_value
                    or (
                        value_keys != settled_keys
                        and value_keys != cursor_keys
                    )
                    or (
                        value_keys == cursor_keys
                        and (
                            not initialized
                            or not isinstance(cursor, str)
                            or not cursor
                            or not cls._cursor(cursor)
                        )
                    )
                ):
                    return None
                clean_value = {"initialized": initialized, "oldest": oldest}
                if value_keys == cursor_keys:
                    clean_value["cursor"] = cursor
                    clean_value["pending_highest"] = pending
                clean_channels[channel_id] = clean_value
            return {
                "version": 2,
                "channels": clean_channels,
                "next_index": next_index,
                "dm_channels": clean_dms,
                "dm_refreshed_at": refreshed,
                **(
                    {"authenticated_user_id": authenticated}
                    if authenticated
                    else {}
                ),
                **(
                    {"legacy_oldest": legacy_oldest}
                    if "legacy_oldest" in state
                    else {}
                ),
            }
        if watermark not in {"", "1970-01-01T00:00:00Z"}:
            try:
                parsed = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return None
            except (TypeError, ValueError, OverflowError):
                return None
        legacy_oldest = "0"
        if watermark:
            try:
                parsed = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
                legacy_oldest = f"{parsed.timestamp():.6f}"
            except (TypeError, ValueError, OverflowError):
                pass
        return {
            "version": 2,
            "channels": {},
            "next_index": 0,
            "dm_channels": [],
            "dm_refreshed_at": "",
            "legacy_oldest": legacy_oldest,
        }

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _timestamp(value: object) -> Decimal | None:
        if not isinstance(value, str) or re.fullmatch(
            r"(?:0|[1-9]\d{0,15})(?:\.\d{1,6})?", value
        ) is None:
            return None
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() and parsed >= 0 else None

    def _refresh_dms(self, token: str, state: dict, max_pages: int) -> bool:
        refreshed = state.get("dm_refreshed_at", "")
        should_refresh = not refreshed
        if refreshed:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(refreshed.replace("Z", "+00:00"))
                should_refresh = age.total_seconds() >= 86400
            except (ValueError, TypeError):
                should_refresh = True
        else:
            should_refresh = True
        if not should_refresh:
            return True
        discovered: list[str] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for page_number in range(max_pages):
            params = {"types": "im", "exclude_archived": "true", "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            result = self._api("conversations.list", token, params)
            if not result or result.get("ok") is not True:
                error = result.get("error") if isinstance(result, dict) else None
                if not isinstance(error, str):
                    error = "request_failed"
                print(f"[slack] cannot refresh DM channels: {error}", file=sys.stderr)
                return bool(state.get("dm_channels"))
            channels = result.get("channels")
            if (
                not isinstance(channels, list)
                or len(channels) > 200
                or not all(
                    isinstance(channel, dict) and _slack_id(channel.get("id"))
                    for channel in channels
                )
            ):
                return False
            discovered.extend(channel["id"] for channel in channels)
            if len(set(discovered)) > 200:
                print(
                    "[slack] more than 200 DM channels discovered; disable automatic "
                    "DM discovery or reduce the bot's accessible conversations",
                    file=sys.stderr,
                )
                return bool(state.get("dm_channels"))
            metadata = result.get("response_metadata", {})
            if not isinstance(metadata, dict):
                return False
            raw_cursor = metadata.get("next_cursor", "")
            if not isinstance(raw_cursor, str) or (
                raw_cursor and not self._cursor(raw_cursor)
            ):
                return False
            cursor = raw_cursor
            if not cursor:
                break
            if cursor in seen_cursors:
                print("[slack] DM discovery repeated a page cursor", file=sys.stderr)
                return bool(state.get("dm_channels"))
            seen_cursors.add(cursor)
            if page_number + 1 >= max_pages:
                print("[slack] DM discovery exceeded dm_discovery_max_pages", file=sys.stderr)
                return bool(state.get("dm_channels"))
        state["dm_channels"] = sorted(set(discovered))
        state["dm_refreshed_at"] = self._utc_now_z()
        return True

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        if not isinstance(config, dict):
            return [], watermark
        token_env = config.get("token_env", "SLACK_TOKEN")
        if (
            not isinstance(token_env, str)
            or len(token_env) > 128
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env)
        ):
            return [], watermark
        token = os.environ.get(token_env, "")
        if (
            not token
            or len(token) > 512
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token)
        ):
            return [], watermark

        watch_dms = config.get("watch_dms", True)
        allow_high_tier = config.get("allow_high_tier_rate_limits", False)
        if type(watch_dms) is not bool or type(allow_high_tier) is not bool:
            print("[slack] watch flags must be booleans", file=sys.stderr)
            return [], watermark

        configured_channels = config.get("watch_channels", [])
        if not isinstance(configured_channels, list):
            print("[slack] watch_channels must be a list", file=sys.stderr)
            return [], watermark
        if (
            len(configured_channels) > 200
            or not all(_slack_id(value) for value in configured_channels)
            or len(set(configured_channels)) != len(configured_channels)
        ):
            print(
                "[slack] watch_channels must contain at most 200 unique channel IDs",
                file=sys.stderr,
            )
            return [], watermark
        configured_user_id = config.get("user_id", "")
        if not isinstance(configured_user_id, str) or (
            configured_user_id and not _slack_id(configured_user_id)
        ):
            print("[slack] user_id must be a Slack user ID", file=sys.stderr)
            return [], watermark
        max_messages = config.get("max_messages", 15)
        requests_per_poll = config.get("history_requests_per_poll", 1)
        dm_discovery_pages = config.get("dm_discovery_max_pages", 10)
        if (
            isinstance(max_messages, bool)
            or not isinstance(max_messages, int)
            or not 1 <= max_messages <= 15
            or isinstance(requests_per_poll, bool)
            or not isinstance(requests_per_poll, int)
            or not 1 <= requests_per_poll <= 50
            or isinstance(dm_discovery_pages, bool)
            or not isinstance(dm_discovery_pages, int)
            or not 1 <= dm_discovery_pages <= 100
        ):
            print("[slack] pagination limits are invalid", file=sys.stderr)
            return [], watermark
        if not allow_high_tier:
            requests_per_poll = 1

        state = self._load_state(watermark)
        if state is None:
            print("[slack] watermark is invalid; preserving it", file=sys.stderr)
            return [], watermark
        if watch_dms and not self._refresh_dms(
            token, state, dm_discovery_pages
        ):
            return [], watermark

        channel_types: dict[str, bool] = {}
        for value in configured_channels:
            channel_types[value] = False
        if watch_dms:
            for value in state.get("dm_channels", []):
                channel_types[value] = True
        channels = sorted(channel_types)
        if not channels:
            return [], self._dump_state(state)

        channel_state = state["channels"]
        next_index = state.get("next_index", 0) % len(channels)
        user_id = configured_user_id or state.get("authenticated_user_id") or ""
        if not user_id:
            auth = self._api("auth.test", token)
            authenticated_id = auth.get("user_id") if isinstance(auth, dict) else None
            if (
                not auth
                or auth.get("ok") is not True
                or not _slack_id(authenticated_id)
            ):
                print("[slack] auth.test could not determine user_id", file=sys.stderr)
                return [], watermark
            user_id = authenticated_id
        if not _slack_id(user_id):
            print("[slack] invalid authenticated user_id", file=sys.stderr)
            return [], watermark
        state["authenticated_user_id"] = user_id
        pollen: list[dict] = []

        for _ in range(min(requests_per_poll, len(channels))):
            channel_id = channels[next_index]
            per_channel = channel_state.get(channel_id, {})
            if not isinstance(per_channel, dict):
                per_channel = {}
            initialized = per_channel.get("initialized") is True
            oldest = str(per_channel.get("oldest") or state.get("legacy_oldest") or "0")
            cursor = str(per_channel.get("cursor") or "")
            params = {
                "channel": channel_id,
                "limit": str(max_messages),
                "inclusive": "false",
            }
            if oldest != "0":
                params["oldest"] = oldest
            if cursor:
                params["cursor"] = cursor

            result = self._api("conversations.history", token, params)
            if not result or result.get("ok") is not True:
                error = result.get("error") if isinstance(result, dict) else None
                if not isinstance(error, str):
                    error = "request_failed"
                print(f"[slack] history error for {channel_id}: {error}", file=sys.stderr)
                # A persistent error in one channel must not pin the global
                # round-robin cursor and starve every later channel.
                next_index = (next_index + 1) % len(channels)
                continue
            messages = result.get("messages")
            if not isinstance(messages, list) or len(messages) > max_messages:
                print(f"[slack] malformed history for {channel_id}", file=sys.stderr)
                next_index = (next_index + 1) % len(channels)
                continue

            has_more = result.get("has_more", False)
            metadata = result.get("response_metadata", {})
            if type(has_more) is not bool or not isinstance(metadata, dict):
                print(f"[slack] malformed pagination for {channel_id}", file=sys.stderr)
                next_index = (next_index + 1) % len(channels)
                continue
            raw_next_cursor = metadata.get("next_cursor", "")
            if not isinstance(raw_next_cursor, str) or (
                raw_next_cursor and not self._cursor(raw_next_cursor)
            ):
                print(f"[slack] malformed pagination for {channel_id}", file=sys.stderr)
                next_index = (next_index + 1) % len(channels)
                continue
            next_cursor = raw_next_cursor
            if has_more != bool(next_cursor) or (cursor and next_cursor == cursor):
                print(
                    f"[slack] contradictory pagination for {channel_id}",
                    file=sys.stderr,
                )
                next_index = (next_index + 1) % len(channels)
                continue

            observed = []
            channel_pollen: list[dict] = []
            page_ids: set[str] = set()
            malformed_page = False
            prior_timestamp: Decimal | None = None
            for msg in messages:
                if not isinstance(msg, dict):
                    malformed_page = True
                    break
                ts = msg.get("ts")
                text = msg.get("text", "")
                raw_user = msg.get("user", "")
                raw_bot = msg.get("bot_id", "")
                thread_ts = msg.get("thread_ts", "")
                profile = msg.get("user_profile", {})
                message_type = msg.get("type")
                parsed_timestamp = self._timestamp(ts)
                if (
                    not isinstance(ts, str)
                    or parsed_timestamp is None
                    or ts in page_ids
                    or (
                        prior_timestamp is not None
                        and parsed_timestamp >= prior_timestamp
                    )
                    or not isinstance(text, str)
                    or len(text) > 100_000
                    or not isinstance(message_type, str)
                    or not 1 <= len(message_type) <= 128
                    or (raw_user != "" and not _slack_id(raw_user))
                    or (raw_bot != "" and not _slack_id(raw_bot))
                    or not isinstance(thread_ts, str)
                    or (thread_ts and self._timestamp(thread_ts) is None)
                    or not isinstance(profile, dict)
                    or not isinstance(profile.get("real_name", ""), str)
                    or len(profile.get("real_name", "")) > 10_000
                    or not isinstance(profile.get("display_name", ""), str)
                    or len(profile.get("display_name", "")) > 10_000
                ):
                    malformed_page = True
                    break
                prior_timestamp = parsed_timestamp
                page_ids.add(ts)
                observed.append(ts)
                if not initialized:
                    continue
                author = raw_user or raw_bot
                if user_id and author == user_id:
                    continue
                is_dm = channel_types[channel_id]
                mentioned = bool(user_id and f"<@{user_id}>" in text)
                is_thread_reply = bool(thread_ts and thread_ts != ts)
                if is_dm:
                    pollen_type = "slack_dm"
                elif mentioned:
                    pollen_type = "slack_mention"
                elif is_thread_reply:
                    # Only thread replies broadcast into channel history are
                    # observable through this polling endpoint.
                    pollen_type = "slack_thread_reply"
                else:
                    continue
                channel_pollen.append({
                    "id": f"slack-{channel_id}-{ts}",
                    "source": "slack",
                    "type": pollen_type,
                    "title": text[:100] or "New Slack message",
                    "preview": text[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": author,
                    "author_name": profile.get("real_name") or profile.get("display_name") or "",
                    "group": "DMs" if is_dm else channel_id,
                    "url": f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
                    "metadata": {
                        "channel_id": channel_id,
                        "ts": ts,
                        "thread_ts": thread_ts,
                        "is_dm": is_dm,
                    },
                })

            if malformed_page:
                print(f"[slack] malformed message page for {channel_id}", file=sys.stderr)
                next_index = (next_index + 1) % len(channels)
                continue

            highest = (
                max(
                    [oldest, *observed],
                    key=lambda value: self._timestamp(value) or Decimal(0),
                )
                if observed
                else oldest
            )
            if not initialized:
                # Bootstrap only the newest page; historical pagination would
                # produce a surprise flood on install.
                per_channel = {"initialized": True, "oldest": highest}
            else:
                pending_highest = str(per_channel.get("pending_highest") or oldest)
                if observed:
                    pending_highest = max(
                        [pending_highest, *observed],
                        key=lambda value: self._timestamp(value) or Decimal(0),
                    )
                if next_cursor:
                    per_channel.update({
                        "initialized": True,
                        "oldest": oldest,
                        "cursor": next_cursor,
                        "pending_highest": pending_highest,
                    })
                else:
                    per_channel = {"initialized": True, "oldest": pending_highest}
            channel_state[channel_id] = per_channel
            pollen.extend(channel_pollen)
            # Preserve any page cursor, but rotate after each request so one
            # busy conversation cannot starve every other watched channel.
            next_index = (next_index + 1) % len(channels)

        state["next_index"] = next_index
        state["channels"] = {
            channel: channel_state[channel]
            for channel in channels
            if channel in channel_state
        }
        state.pop("legacy_oldest", None)
        return pollen, self._dump_state(state)


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = SlackScanner()
    if data.get("command") == "poll":
        result_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
