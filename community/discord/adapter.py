"""Discord scanner for explicitly configured guild and DM channel IDs."""

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
from typing import Optional, Union


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


def _snowflake(value: object, *, allow_zero: bool = False) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and 1 <= len(value) <= 20
        and (allow_zero or value != "0")
    )


def _valid_channel_state(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("initialized"), bool):
        return False
    if not _snowflake(value.get("after"), allow_zero=True):
        return False
    for key in ("backfill_before", "pending_highest"):
        if key in value and not _snowflake(value[key]):
            return False
    return True


def _normalize_message(value: object) -> tuple[dict, str, int] | None:
    if not isinstance(value, dict):
        return None
    message_id = value.get("id")
    content = value.get("content")
    author = value.get("author")
    mentions = value.get("mentions")
    attachments = value.get("attachments")
    guild_id = value.get("guild_id", "")
    if guild_id is None:
        guild_id = ""
    if (
        not _snowflake(message_id)
        or not isinstance(content, str)
        or len(content) > 20_000
        or not isinstance(author, dict)
        or not _snowflake(author.get("id"))
        or not isinstance(author.get("username"), str)
        or len(author["username"]) > 1_000
        or (
            author.get("global_name") is not None
            and (
                not isinstance(author.get("global_name"), str)
                or len(author["global_name"]) > 1_000
            )
        )
        or ("bot" in author and not isinstance(author["bot"], bool))
        or not isinstance(mentions, list)
        or len(mentions) > 1_000
        or not all(
            isinstance(mention, dict) and _snowflake(mention.get("id"))
            for mention in mentions
        )
        or not isinstance(attachments, list)
        or len(attachments) > 1_000
        or not all(
            isinstance(attachment, dict)
            and isinstance(attachment.get("filename"), str)
            and len(attachment["filename"]) <= 10_000
            for attachment in attachments
        )
        or (guild_id != "" and not _snowflake(guild_id))
    ):
        return None
    normalized = dict(value)
    normalized["guild_id"] = guild_id
    return normalized, message_id, int(message_id)


class DiscordScanner:
    name = "discord"
    _POLL_BUDGET_SECONDS = 45

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "DISCORD_BOT_TOKEN",
            "watch_channels": [],
            # Discord has no REST operation that lists a bot's DM channels.
            "watch_dm_channels": [],
            "watch_dms": False,
            "user_id": "",
            "bot_user_id": "",
            "max_messages": 100,
            "channels_per_poll": 10,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(
        self, endpoint: str, token: str, params: Optional[dict] = None
    ) -> Optional[Union[list, dict]]:
        query = urllib.parse.urlencode(params or {})
        url = f"https://discord.com/api/v10{endpoint}" + (f"?{query}" if query else "")
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bot {token}", "Accept": "application/json"},
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
                return _strict_json(raw)
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "")
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[discord] HTTP {exc.code} ({endpoint}){suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[discord] API error ({endpoint}): {exc}", file=sys.stderr)
        return None

    @staticmethod
    def _load_state(watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") == 2:
            channels = state.get("channels")
            next_index = state.get("next_index")
            if (
                not isinstance(channels, dict)
                or len(channels) > 200
                or not all(
                    _snowflake(channel_id) and _valid_channel_state(value)
                    for channel_id, value in channels.items()
                )
                or isinstance(next_index, bool)
                or not isinstance(next_index, int)
                or not 0 <= next_index <= 1_000_000
                or (
                    "legacy_after" in state
                    and not _snowflake(state["legacy_after"], allow_zero=True)
                )
            ):
                return None
            return state
        if isinstance(watermark, str) and watermark.lstrip().startswith(("{", "[")):
            return None
        legacy = "0"
        try:
            legacy = str(max(0, int(watermark or 0)))
        except (TypeError, ValueError):
            pass
        return {"version": 2, "channels": {}, "next_index": 0, "legacy_after": legacy}

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        token_env = config.get("token_env", "DISCORD_BOT_TOKEN")
        if not isinstance(token_env, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", token_env
        ):
            print("[discord] invalid token_env", file=sys.stderr)
            return [], watermark
        token = os.environ.get(token_env, "")
        if (
            not token
            or len(token) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in token)
        ):
            return [], watermark

        watch_dms = config.get("watch_dms", False)
        if type(watch_dms) is not bool:
            print("[discord] watch_dms must be a boolean", file=sys.stderr)
            return [], watermark

        configured_guilds = config.get("watch_channels", [])
        configured_dms = config.get("watch_dm_channels", [])
        if (
            not isinstance(configured_guilds, list)
            or not isinstance(configured_dms, list)
            or len(configured_guilds) + len(configured_dms) > 200
            or not all(
                isinstance(value, str)
                for value in [*configured_guilds, *configured_dms]
            )
        ):
            print("[discord] channel lists must contain at most 200 strings", file=sys.stderr)
            return [], watermark
        all_channel_values = [*configured_guilds, *configured_dms]
        if not all(_snowflake(value) for value in all_channel_values):
            print("[discord] channel IDs must be numeric snowflakes", file=sys.stderr)
            return [], watermark
        guild_channels = set(configured_guilds)
        dm_channels = set(configured_dms)
        if watch_dms and not dm_channels:
            print(
                "[discord] watch_dms requires explicit watch_dm_channels; Discord cannot list them via REST",
                file=sys.stderr,
            )
        channel_types = {channel: False for channel in guild_channels}
        if watch_dms:
            channel_types.update({channel: True for channel in dm_channels})
        channels = sorted(channel_types)
        if not channels:
            return [], watermark
        max_messages = config.get("max_messages", 100)
        channels_per_poll = config.get("channels_per_poll", 10)
        if (
            isinstance(max_messages, bool)
            or not isinstance(max_messages, int)
            or not 1 <= max_messages <= 100
            or isinstance(channels_per_poll, bool)
            or not isinstance(channels_per_poll, int)
            or not 1 <= channels_per_poll <= 100
        ):
            print("[discord] pagination limits are invalid", file=sys.stderr)
            return [], watermark

        state = self._load_state(watermark)
        if state is None:
            print("[discord] invalid persisted state; preserving watermark", file=sys.stderr)
            return [], watermark
        per_channel_state = state["channels"]
        next_index = state.get("next_index", 0) % len(channels)
        initial_next_index = next_index
        user_id = config.get("user_id", "")
        bot_user_id = config.get("bot_user_id", "")
        if not isinstance(user_id, str) or not isinstance(bot_user_id, str):
            print("[discord] user IDs must be strings", file=sys.stderr)
            return [], watermark
        if any(
            value and (not value.isdigit() or len(value) > 20)
            for value in (user_id, bot_user_id)
        ):
            print("[discord] user IDs must be numeric snowflakes", file=sys.stderr)
            return [], watermark
        pollen: list[dict] = []
        successful_channels = 0

        for _ in range(min(channels_per_poll, len(channels))):
            channel_id = channels[next_index]
            channel_state = per_channel_state.get(channel_id, {})
            if not isinstance(channel_state, dict):
                channel_state = {}
            legacy_after = str(state.get("legacy_after") or "0")
            initialized = bool(channel_state.get("initialized")) or legacy_after != "0"
            after = str(channel_state.get("after") or legacy_after)
            backfill_before = str(channel_state.get("backfill_before") or "")
            pending_highest = str(channel_state.get("pending_highest") or after)
            params = {"limit": str(max_messages)}
            if backfill_before:
                params["before"] = backfill_before
            elif after != "0":
                params["after"] = after
            messages = self._api(f"/channels/{urllib.parse.quote(channel_id, safe='')}/messages", token, params)
            if not isinstance(messages, list):
                next_index = (next_index + 1) % len(channels)
                continue

            try:
                committed_numeric = int(after)
                pending_numeric = max(committed_numeric, int(pending_highest))
            except (TypeError, ValueError):
                print(f"[discord] invalid saved cursor for {channel_id}", file=sys.stderr)
                next_index = (next_index + 1) % len(channels)
                continue
            parsed_messages: list[tuple[dict, str, int]] = []
            malformed_page = False
            for msg in messages:
                normalized = _normalize_message(msg)
                if (
                    normalized is None
                    or (
                        parsed_messages
                        and normalized[2] >= parsed_messages[-1][2]
                    )
                ):
                    malformed_page = True
                    break
                parsed_messages.append(normalized)
            if malformed_page:
                print(f"[discord] malformed message page for {channel_id}", file=sys.stderr)
                next_index = (next_index + 1) % len(channels)
                continue

            highest = pending_numeric
            relevant_ids: list[int] = []
            is_dm = channel_types[channel_id]
            channel_pollen: list[dict] = []
            for msg, msg_id, numeric_id in parsed_messages:
                highest = max(highest, numeric_id)
                if numeric_id > committed_numeric:
                    relevant_ids.append(numeric_id)
                else:
                    # A before-page reached the already committed boundary.
                    continue
                if not initialized:
                    continue
                author = msg["author"]
                author_id = author["id"]
                if author.get("bot") is True or (bot_user_id and author_id == bot_user_id):
                    continue
                content = msg["content"]
                if not content:
                    attachments = msg["attachments"]
                    attachment_names = [
                        value["filename"]
                        for value in attachments[:5]
                        if value["filename"]
                    ]
                    if attachment_names:
                        content = "Attachment: " + ", ".join(attachment_names)
                mentions = msg["mentions"]
                mentioned = bool(user_id) and (
                    any(value["id"] == user_id for value in mentions)
                    or f"<@{user_id}>" in content
                    or f"<@!{user_id}>" in content
                )
                if is_dm:
                    pollen_type = "discord_dm"
                elif mentioned:
                    pollen_type = "discord_mention"
                else:
                    continue

                guild_id = msg["guild_id"]
                url = (
                    f"https://discord.com/channels/@me/{channel_id}/{msg_id}"
                    if is_dm
                    else (
                        f"https://discord.com/channels/{guild_id}/{channel_id}/{msg_id}"
                        if guild_id
                        else ""
                    )
                )
                author_name = author.get("global_name") or author["username"]
                channel_pollen.append({
                    "id": f"discord-{msg_id}",
                    "source": "discord",
                    "type": pollen_type,
                    "title": f"{author_name}: {content[:80]}" if author_name else (content[:100] or "New Discord message"),
                    "preview": content[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": author_id,
                    "author_name": author_name,
                    "group": "DMs" if is_dm else channel_id,
                    "url": url,
                    "metadata": {
                        "channel_id": channel_id,
                        "guild_id": guild_id,
                        "author_username": author["username"],
                    },
                })

            if not initialized:
                # Bootstrap only the newest page and intentionally discard
                # history so installing the scanner cannot flood the queue.
                per_channel_state[channel_id] = {
                    "initialized": True,
                    "after": str(highest),
                }
            elif backfill_before:
                # Discord returns newest-to-oldest. Walk backward until the
                # committed boundary before advancing `after`; otherwise a
                # burst larger than 100 messages loses its middle pages.
                reached_boundary = (
                    len(messages) < max_messages
                    or any(
                        isinstance(msg, dict)
                        and str(msg.get("id") or "").isdigit()
                        and int(str(msg.get("id"))) <= committed_numeric
                        for msg in messages
                    )
                    or not relevant_ids
                )
                if reached_boundary:
                    per_channel_state[channel_id] = {
                        "initialized": True,
                        "after": str(highest),
                    }
                else:
                    per_channel_state[channel_id] = {
                        "initialized": True,
                        "after": after,
                        "backfill_before": str(min(relevant_ids)),
                        "pending_highest": str(highest),
                    }
            elif len(messages) >= max_messages and relevant_ids:
                per_channel_state[channel_id] = {
                    "initialized": True,
                    "after": after,
                    "backfill_before": str(min(relevant_ids)),
                    "pending_highest": str(highest),
                }
            else:
                per_channel_state[channel_id] = {
                    "initialized": True,
                    "after": str(highest),
                }
            pollen.extend(channel_pollen)
            successful_channels += 1
            # A channel with a multi-page burst retains its backfill cursor but
            # rotates behind the other channels after each request.
            next_index = (next_index + 1) % len(channels)

        if successful_channels == 0 and next_index == initial_next_index:
            return [], watermark
        state["next_index"] = next_index
        state["channels"] = {
            channel: per_channel_state[channel]
            for channel in channels
            if channel in per_channel_state
        }
        state.pop("legacy_after", None)
        return pollen, self._dump_state(state)


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = DiscordScanner()
    if data.get("command") == "poll":
        result_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
