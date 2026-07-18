"""Telegram scanner — monitors Telegram messages and mentions via Bot API."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional


_MAX_TELEGRAM_ID = (1 << 53) - 1
_MAX_UPDATE_ID = (1 << 63) - 1
_SUPPORTED_MESSAGE_UPDATES = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "business_message",
    "edited_business_message",
    "guest_message",
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


def _provider_id(value: object, *, signed: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    lower = -_MAX_TELEGRAM_ID if signed else 1
    return lower <= value <= _MAX_TELEGRAM_ID and (signed or value > 0)


def _bounded_string(value: object, maximum: int) -> bool:
    return isinstance(value, str) and len(value) <= maximum and "\x00" not in value


def _normalize_user(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    if (
        not _provider_id(value.get("id"))
        or not isinstance(value.get("is_bot"), bool)
        or not _bounded_string(value.get("first_name"), 1_000)
        or (
            "username" in value
            and not _bounded_string(value.get("username"), 1_000)
        )
    ):
        return None
    return value


def _normalize_chat(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    if (
        not _provider_id(value.get("id"), signed=True)
        or value.get("id") == 0
        or value.get("type") not in {"private", "group", "supergroup", "channel"}
        or any(
            key in value and not _bounded_string(value.get(key), 10_000)
            for key in ("title", "username", "first_name", "last_name")
        )
    ):
        return None
    return value


def _normalize_message(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    message_id = value.get("message_id")
    sent_at = value.get("date")
    chat = _normalize_chat(value.get("chat"))
    if (
        isinstance(message_id, bool)
        or not isinstance(message_id, int)
        or not 0 <= message_id <= _MAX_UPDATE_ID
        or isinstance(sent_at, bool)
        or not isinstance(sent_at, int)
        or not 0 < sent_at <= 253_402_300_799
        or chat is None
        or any(
            key in value and not _bounded_string(value.get(key), 20_000)
            for key in ("text", "caption", "author_signature")
        )
    ):
        return None

    from_user = None
    if "from" in value:
        from_user = _normalize_user(value.get("from"))
        if from_user is None:
            return None
    sender_chat = None
    if "sender_chat" in value:
        sender_chat = _normalize_chat(value.get("sender_chat"))
        if sender_chat is None:
            return None

    replied_to = value.get("reply_to_message")
    if replied_to is not None:
        if not isinstance(replied_to, dict):
            return None
        if "from" in replied_to and _normalize_user(replied_to.get("from")) is None:
            return None

    normalized = dict(value)
    normalized["chat"] = chat
    normalized["_from"] = from_user
    normalized["_sender_chat"] = sender_chat
    return normalized


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


class TelegramScanner:
    name = "telegram"
    _POLL_BUDGET_SECONDS = 45

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "TELEGRAM_BOT_TOKEN",
            "watch_chats": [],
            "max_messages": 20,
            "bot_username": "",
            "bot_user_id": "",
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _load_state(watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") == 3:
            offset = state.get("last_update_id")
            initialized = state.get("initialized")
            last_update_seen_at = state.get("last_update_seen_at")
            if (
                set(state)
                != {
                    "version",
                    "last_update_id",
                    "initialized",
                    "last_update_seen_at",
                }
                or isinstance(state.get("version"), bool)
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or not 0 <= offset <= _MAX_UPDATE_ID
                or not isinstance(initialized, bool)
                or (not initialized and offset != 0)
                or not isinstance(last_update_seen_at, str)
                or (
                    last_update_seen_at
                    and TelegramScanner._parse_timestamp(last_update_seen_at) is None
                )
            ):
                return None
            return dict(state)
        if isinstance(state, dict) and state.get("version") == 2:
            offset = state.get("last_update_id")
            initialized = state.get("initialized")
            if (
                set(state) != {"version", "last_update_id", "initialized"}
                or isinstance(state.get("version"), bool)
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or not 0 <= offset <= _MAX_UPDATE_ID
                or not isinstance(initialized, bool)
                or (not initialized and offset != 0)
            ):
                return None
            return {
                "version": 3,
                "last_update_id": offset,
                "initialized": initialized,
                # Probe once when migrating because v2 did not record how long
                # the queue had been quiet.
                "last_update_seen_at": "",
            }
        if not isinstance(watermark, str):
            return None
        if watermark in {"", "1970-01-01T00:00:00Z"}:
            return {
                "version": 3,
                "last_update_id": 0,
                "initialized": False,
                "last_update_seen_at": "",
            }
        if not re.fullmatch(r"0|[1-9]\d{0,18}", watermark):
            return None
        legacy_offset = int(watermark)
        if legacy_offset > _MAX_UPDATE_ID:
            return None
        return {
            "version": 3,
            "last_update_id": legacy_offset,
            "initialized": True,
            "last_update_seen_at": "",
        }

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 64
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return None
        parsed = parsed.astimezone(timezone.utc)
        if parsed > datetime.now(timezone.utc) + timedelta(minutes=5):
            return None
        return parsed

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    def _api(self, method: str, token: str, params: Optional[dict] = None) -> Optional[dict]:
        """Call a Telegram Bot API method with GET request."""
        encoded_token = urllib.parse.quote(token, safe=":")
        url = f"https://api.telegram.org/bot{encoded_token}/{method}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"
        req = urllib.request.Request(url)
        deadline = getattr(self, "_poll_deadline", None)
        timeout = 15.0
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            timeout = min(timeout, max(0.1, remaining))
        try:
            with _urlopen(req, timeout=timeout) as resp:
                raw = resp.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("response exceeded 1 MB")
                result = _strict_json(raw)
                return result if isinstance(result, dict) else None
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "")
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[telegram] HTTP {exc.code} ({method}){suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[telegram] API error ({method}): {exc}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        if not isinstance(config, dict):
            return [], watermark
        token_env = config.get("token_env", "TELEGRAM_BOT_TOKEN")
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

        max_messages = config.get("max_messages", 20)
        if (
            isinstance(max_messages, bool)
            or not isinstance(max_messages, int)
            or not 1 <= max_messages <= 100
        ):
            print("[telegram] max_messages must be an integer from 1 to 100", file=sys.stderr)
            return [], watermark
        watch_chats = config.get("watch_chats", [])
        if (
            not isinstance(watch_chats, list)
            or len(watch_chats) > 1000
            or not all(
                isinstance(value, (str, int)) and not isinstance(value, bool)
                for value in watch_chats
            )
        ):
            return [], watermark
        watched_values: list[str] = []
        for value in watch_chats:
            normalized = str(value)
            if (
                not re.fullmatch(r"-?(?:0|[1-9]\d{0,15})", normalized)
                or normalized == "-0"
                or int(normalized) == 0
                or abs(int(normalized)) > _MAX_TELEGRAM_ID
            ):
                print("[telegram] watch_chats entries must be numeric chat IDs", file=sys.stderr)
                return [], watermark
            watched_values.append(normalized)
        if len(set(watched_values)) != len(watched_values):
            print("[telegram] watch_chats entries must be unique", file=sys.stderr)
            return [], watermark
        watched = set(watched_values)

        configured_username = config.get("bot_username", "")
        configured_user_id = config.get("bot_user_id", "")
        if (
            not isinstance(configured_username, str)
            or not isinstance(configured_user_id, (str, int))
            or isinstance(configured_user_id, bool)
        ):
            print("[telegram] invalid bot identity", file=sys.stderr)
            return [], watermark
        bot_username = (
            configured_username[1:]
            if configured_username.startswith("@")
            else configured_username
        )
        bot_user_id = str(configured_user_id)
        if (
            (bot_username and not re.fullmatch(r"[A-Za-z0-9_]{1,64}", bot_username))
            or (
                bot_user_id
                and (
                    not re.fullmatch(r"[1-9]\d{0,15}", bot_user_id)
                    or int(bot_user_id) > _MAX_TELEGRAM_ID
                )
            )
        ):
            print("[telegram] invalid bot identity", file=sys.stderr)
            return [], watermark

        state = self._load_state(watermark)
        if state is None:
            print("[telegram] invalid watermark state", file=sys.stderr)
            return [], watermark
        last_update_id = int(state["last_update_id"])
        initialized = bool(state["initialized"])
        last_update_seen_at = state["last_update_seen_at"]
        last_seen = self._parse_timestamp(last_update_seen_at)
        # Telegram documents that the first update ID after at least a quiet
        # week is random rather than sequential. Sending the old ID + 1 in
        # that situation can acknowledge a lower random ID without returning
        # it. Probe the unconfirmed queue without an offset before that point.
        quiet_queue_probe = initialized and (
            last_seen is None
            or datetime.now(timezone.utc) - last_seen >= timedelta(days=6)
        )
        params = {
            "limit": "1" if not initialized else str(max_messages),
            "allowed_updates": json.dumps(
                list(_SUPPORTED_MESSAGE_UPDATES),
                separators=(",", ":"),
            ),
        }
        if not initialized:
            # Telegram defines a negative offset as an index from the end of
            # the queue and forgets all preceding updates. Snapshot the newest
            # update atomically instead of trying to drain an unbounded live
            # queue during first install.
            params["offset"] = "-1"
        elif last_update_id and not quiet_queue_probe:
            params["offset"] = str(last_update_id + 1)

        result = self._api("getUpdates", token, params)
        if not result or result.get("ok") is not True:
            if isinstance(result, dict):
                description = result.get("description")
                if not isinstance(description, str):
                    description = "unknown error"
                print(
                    f"[telegram] getUpdates rejected: {description[:200]}",
                    file=sys.stderr,
                )
            return [], watermark

        updates = result.get("result")
        requested_limit = 1 if not initialized else max_messages
        if not isinstance(updates, list) or len(updates) > requested_limit:
            print("[telegram] malformed update result", file=sys.stderr)
            return [], watermark
        if not updates:
            state["initialized"] = True
            return [], self._dump_state(state)

        parsed_updates: list[tuple[int, str, dict | None]] = []
        # Telegram may choose a new random update identifier after at least a
        # week without updates. Enforce ordering within this response, but do
        # not assume it must remain above the previously confirmed identifier.
        previous_update_id = -1
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if (
                not isinstance(update, dict)
                or not isinstance(update_id, int)
                or isinstance(update_id, bool)
                or not 0 < update_id <= _MAX_UPDATE_ID
                or update_id <= previous_update_id
            ):
                print("[telegram] malformed update batch", file=sys.stderr)
                return [], watermark
            present_types = [key for key in _SUPPORTED_MESSAGE_UPDATES if key in update]
            if len(present_types) > 1:
                print("[telegram] malformed update batch", file=sys.stderr)
                return [], watermark
            update_type = present_types[0] if present_types else ""
            message = _normalize_message(update.get(update_type)) if update_type else None
            if update_type and message is None:
                print("[telegram] malformed message update", file=sys.stderr)
                return [], watermark
            parsed_updates.append((update_id, update_type, message))
            previous_update_id = update_id

        if not initialized:
            return [], self._dump_state({
                "version": 3,
                "last_update_id": parsed_updates[0][0],
                "initialized": True,
                "last_update_seen_at": self._utc_now_z(),
            })

        candidate_messages = [
            message
            for _, update_type, message in parsed_updates
            if update_type
            and message is not None
            and (not watched or str(message["chat"]["id"]) in watched)
        ]
        has_supported_message = bool(candidate_messages)
        configured_bot_username = bot_username
        configured_bot_user_id = bot_user_id
        # Identity is needed to suppress the bot's own messages and classify
        # replies. Avoid the extra request for unsupported or unwatched batches.
        if has_supported_message and (not bot_username or not bot_user_id):
            me_result = self._api("getMe", token)
            me = (
                me_result.get("result")
                if isinstance(me_result, dict) and me_result.get("ok") is True
                else None
            )
            normalized_me = _normalize_user(me)
            if normalized_me is None or normalized_me.get("is_bot") is not True:
                print("[telegram] getMe could not determine bot identity", file=sys.stderr)
                return [], watermark
            fetched_username = normalized_me.get("username")
            if (
                not isinstance(fetched_username, str)
                or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", fetched_username)
            ):
                print("[telegram] getMe returned an invalid bot identity", file=sys.stderr)
                return [], watermark
            fetched_user_id = str(normalized_me["id"])
            if (
                configured_bot_username
                and configured_bot_username.casefold() != fetched_username.casefold()
            ) or (
                configured_bot_user_id and configured_bot_user_id != fetched_user_id
            ):
                print("[telegram] configured bot identity does not match token", file=sys.stderr)
                return [], watermark
            bot_username = fetched_username
            bot_user_id = fetched_user_id
        if has_supported_message and (
            not re.fullmatch(r"[A-Za-z0-9_]{1,64}", bot_username)
            or not re.fullmatch(r"[1-9]\d{0,15}", bot_user_id)
            or int(bot_user_id) > _MAX_TELEGRAM_ID
        ):
            print("[telegram] getMe returned an invalid bot identity", file=sys.stderr)
            return [], watermark

        pollen = []
        for numeric_update_id, update_type, msg in parsed_updates:
            # Every update must be acknowledged, including unsupported or
            # unwatched updates, or it remains at the head of the Bot API queue.
            last_update_id = numeric_update_id
            if not update_type:
                continue
            assert msg is not None
            chat = msg["chat"]
            chat_id = chat["id"]

            # Filter by watch_chats if configured
            if watched and str(chat_id) not in watched:
                continue

            from_user = msg["_from"] or msg["_sender_chat"] or {}
            if bot_user_id and str(from_user.get("id") or "") == bot_user_id:
                continue
            text = msg.get("text") or msg.get("caption") or ""
            if not text:
                text = "Non-text Telegram message"
            first_name = from_user.get("first_name") or ""
            if not first_name:
                first_name = (
                    from_user.get("title")
                    or from_user.get("username")
                    or msg.get("author_signature")
                    or chat.get("title")
                    or chat.get("first_name")
                    or chat.get("username")
                    or str(chat_id)
                )

            # Pollen type detection
            is_mention = False
            if bot_username and re.search(
                rf"(?<![\w@])@{re.escape(bot_username)}(?!\w)",
                str(text),
                re.IGNORECASE,
            ):
                is_mention = True
            replied_to = msg.get("reply_to_message")
            if isinstance(replied_to, dict):
                replied_user = replied_to.get("from")
                if isinstance(replied_user, dict):
                    replied_id = str(replied_user.get("id") or "")
                    replied_username = str(replied_user.get("username") or "")
                    if (bot_user_id and replied_id == bot_user_id) or (
                        bot_username and replied_username.casefold() == bot_username.casefold()
                    ):
                        is_mention = True

            pollen_type = "telegram_mention" if is_mention else "telegram_message"

            pollen_id = f"telegram-{numeric_update_id}"
            title = f"{first_name}: {text[:80]}"

            pollen.append({
                "id": pollen_id,
                "source": "telegram",
                "type": pollen_type,
                "title": title,
                "preview": text[:200],
                "discovered_at": self._utc_now_z(),
                "author": from_user.get("username", ""),
                "author_name": first_name,
                "group": (
                    chat.get("title")
                    or chat.get("first_name")
                    or chat.get("username")
                    or str(chat_id)
                ),
                "url": "",
                "metadata": {
                    "chat_id": chat_id,
                    "chat_title": chat.get("title", ""),
                    "from_username": from_user.get("username", ""),
                    "update_type": update_type,
                },
            })

        return pollen, self._dump_state({
            "version": 3,
            "last_update_id": last_update_id,
            "initialized": True,
            "last_update_seen_at": self._utc_now_z(),
        })


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = TelegramScanner()
    if data.get("command") == "poll":
        result_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
