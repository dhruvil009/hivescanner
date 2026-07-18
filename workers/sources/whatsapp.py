"""WhatsApp scanner — watches incoming messages via `whatsapp-cli`."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# Resolve imports whether run as module or standalone
try:
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists
except ImportError:
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists


class WhatsAppScanner:
    name = "whatsapp"

    def __init__(self):
        self._cli_available = None
        self._snapshot = load_snapshot("whatsapp_messages")
        self._bootstrapped = snapshot_exists("whatsapp_messages")

    def configure(self) -> dict:
        return {
            "enabled": False,
            "watch_chats": [],
            "max_messages": 20,
            "max_pages_per_poll": 100,
            "store_path": "",
        }

    def _wa(self, args: list[str], timeout: int = 15) -> str | None:
        """Run whatsapp-cli command, return stdout or None on failure."""
        try:
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                result = subprocess.run(
                    ["whatsapp-cli"] + args,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout,
                )
                stdout_file.seek(0)
                raw_stdout = stdout_file.read(5_000_001)
                stderr_file.seek(0)
                raw_stderr = stderr_file.read(201)
            if result.returncode != 0:
                print(
                    f"[whatsapp] whatsapp-cli error: {raw_stderr.decode('utf-8', errors='replace')}",
                    file=sys.stderr,
                )
                return None
            if len(raw_stdout) > 5_000_000:
                print("[whatsapp] whatsapp-cli output exceeded 5 MB", file=sys.stderr)
                return None
            return raw_stdout.decode("utf-8")
        except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError, ValueError) as e:
            print(f"[whatsapp] whatsapp-cli failed: {e}", file=sys.stderr)
            return None

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @staticmethod
    def _bounded_int(value: object, *, minimum: int, maximum: int) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if minimum <= value <= maximum else None

    @staticmethod
    def _message_key(chat_jid: str, msg_id: str) -> str:
        return hashlib.sha256(f"{chat_jid}\0{msg_id}".encode()).hexdigest()

    @classmethod
    def _valid_boundary(cls, value: object, *, legacy: bool = False) -> dict | None:
        allowed_keys = {
            "initialized",
            "boundary_time",
            "ids_at_boundary",
            "legacy_ids_at_boundary",
        }
        required_keys = allowed_keys - ({"legacy_ids_at_boundary"} if legacy else set())
        if (
            not isinstance(value, dict)
            or not required_keys.issubset(value)
            or not set(value).issubset(allowed_keys)
            or (not legacy and set(value) != allowed_keys)
        ):
            return None
        initialized = value.get("initialized")
        boundary_time = value.get("boundary_time", "")
        boundary_ids = value.get("ids_at_boundary", [])
        legacy_ids = value.get("legacy_ids_at_boundary", [])
        if (
            not isinstance(initialized, bool)
            or not isinstance(boundary_time, str)
            or not isinstance(boundary_ids, list)
            or not isinstance(legacy_ids, list)
            or len(boundary_ids) + len(legacy_ids) > 1_000
        ):
            return None
        if (
            not all(isinstance(item, str) for item in boundary_ids)
            or not all(isinstance(item, str) for item in legacy_ids)
            or len(set(boundary_ids)) != len(boundary_ids)
            or len(set(legacy_ids)) != len(legacy_ids)
        ):
            return None
        normalized_boundary = cls._normalize_timestamp(boundary_time) if boundary_time else ""
        if initialized and not normalized_boundary:
            return None
        if not initialized and (boundary_time or boundary_ids or legacy_ids):
            return None
        if legacy:
            raw_legacy_ids = boundary_ids
            boundary_ids = []
            legacy_ids = [*legacy_ids, *raw_legacy_ids]
        if not all(
            isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
            for item in boundary_ids
        ):
            return None
        if not all(
            isinstance(item, str)
            and item
            and len(item) <= 256
            and not any(ord(char) < 32 or ord(char) == 127 for char in item)
            for item in legacy_ids
        ):
            return None
        return {
            "initialized": initialized,
            "boundary_time": normalized_boundary,
            "ids_at_boundary": sorted(set(boundary_ids)),
            "legacy_ids_at_boundary": sorted(set(legacy_ids)),
        }

    @classmethod
    def _candidate_committed(cls, watermark: str, candidate_watermark: str) -> bool:
        current = cls._normalize_timestamp(watermark)
        candidate = cls._normalize_timestamp(candidate_watermark)
        return bool(current and candidate and current >= candidate)

    @staticmethod
    def _strict_json(raw: str) -> object | None:
        def reject_constant(value: str) -> None:
            raise ValueError(value)

        def strict_object(pairs: list[tuple[str, object]]) -> dict:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate key: {key}")
                result[key] = value
            return result

        try:
            return json.loads(
                raw,
                parse_constant=reject_constant,
                object_pairs_hook=strict_object,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        if not isinstance(config, dict) or not isinstance(watermark, str):
            return [], watermark
        if watermark and self._normalize_timestamp(watermark) is None:
            print("[whatsapp] invalid watermark; preserving it", file=sys.stderr)
            return [], watermark

        max_messages = self._bounded_int(
            config.get("max_messages", 20), minimum=1, maximum=500
        )
        max_pages = self._bounded_int(
            config.get("max_pages_per_poll", 100), minimum=1, maximum=100
        )
        watch_chats = config.get("watch_chats", [])
        raw_store_path = config.get("store_path", "")
        if max_messages is None or max_pages is None:
            print("[whatsapp] invalid message paging limits", file=sys.stderr)
            return [], watermark
        if (
            not isinstance(watch_chats, list)
            or len(watch_chats) > 500
            or not all(isinstance(value, str) for value in watch_chats)
            or any(not value or value != value.strip() for value in watch_chats)
            or len(set(watch_chats)) != len(watch_chats)
            or any(
                len(value) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                for value in watch_chats
            )
        ):
            print(
                "[whatsapp] watch_chats must be a unique list of bounded chat IDs",
                file=sys.stderr,
            )
            return [], watermark
        if (
            not isinstance(raw_store_path, str)
            or raw_store_path != raw_store_path.strip()
            or len(raw_store_path) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in raw_store_path)
        ):
            print("[whatsapp] invalid store_path", file=sys.stderr)
            return [], watermark
        watched = set(watch_chats)
        store_path = raw_store_path
        global_args = ["--store", store_path] if store_path else []

        if self._cli_available is None:
            self._cli_available = ensure_tool("whatsapp-cli")
        if not self._cli_available:
            return [], watermark

        if not isinstance(self._snapshot, dict):
            print("[whatsapp] invalid persisted snapshot; preserving watermark", file=sys.stderr)
            return [], watermark
        schema_version = self._snapshot.get("schema_version")
        if type(schema_version) is int and schema_version in {3, 4}:
            legacy_state = schema_version == 3
            if set(self._snapshot) != {
                "schema_version",
                "committed",
                "candidate",
                "candidate_watermark",
                "bootstrap_pending",
            }:
                print("[whatsapp] invalid persisted snapshot; preserving watermark", file=sys.stderr)
                return [], watermark
            committed = self._valid_boundary(
                self._snapshot.get("committed"), legacy=legacy_state
            )
            candidate = self._valid_boundary(
                self._snapshot.get("candidate"), legacy=legacy_state
            )
            raw_candidate = self._snapshot.get("candidate")
            bootstrap_pending = self._snapshot.get("bootstrap_pending")
            candidate_wm = self._snapshot.get("candidate_watermark")
            unstaged = raw_candidate == {} and candidate_wm == ""
            if (
                committed is None
                or not isinstance(bootstrap_pending, bool)
                or not isinstance(candidate_wm, str)
                or (
                    unstaged
                    and bootstrap_pending
                )
                or (
                    not unstaged
                    and (
                        not candidate_wm
                        or self._normalize_timestamp(candidate_wm) is None
                        or candidate is None
                    )
                )
                or (
                    candidate_wm == ""
                    and raw_candidate != {}
                )
            ):
                print("[whatsapp] invalid persisted snapshot; preserving watermark", file=sys.stderr)
                return [], watermark
            if candidate_wm and self._candidate_committed(watermark, candidate_wm):
                assert candidate is not None
                committed = candidate
            if (
                bootstrap_pending
                and candidate_wm
                and self._candidate_committed(watermark, candidate_wm)
            ):
                bootstrap_pending = False
        elif "schema_version" in self._snapshot:
            print("[whatsapp] unsupported snapshot version; preserving watermark", file=sys.stderr)
            return [], watermark
        else:
            # Migrate the old scalar watermark without replaying everything.
            if set(self._snapshot) == {"messages"} and isinstance(
                self._snapshot.get("messages"), dict
            ):
                legacy_messages = self._snapshot["messages"]
            else:
                legacy_messages = self._snapshot
            if (
                len(legacy_messages) > 10_000
                or not all(
                    isinstance(key, str)
                    and key
                    and len(key) <= 256
                    and not any(ord(char) < 32 or ord(char) == 127 for char in key)
                    and self._normalize_timestamp(value) is not None
                    for key, value in legacy_messages.items()
                )
            ):
                print("[whatsapp] invalid legacy snapshot; preserving watermark", file=sys.stderr)
                return [], watermark
            raw_legacy_boundary = (
                watermark
                if isinstance(watermark, str) and not watermark.startswith("1970-")
                else ""
            )
            legacy_boundary = self._normalize_timestamp(raw_legacy_boundary) or ""
            legacy_boundary_ids = []
            if legacy_boundary:
                legacy_boundary_ids = [
                    key.rsplit(":", 1)[-1]
                    for key, value in legacy_messages.items()
                    if (
                        isinstance(key, str)
                        and key
                        and len(key) <= 256
                        and not any(ord(char) < 32 or ord(char) == 127 for char in key)
                        and self._normalize_timestamp(value) == legacy_boundary
                    )
                ]
            committed = {
                "initialized": bool(self._bootstrapped and legacy_boundary),
                "boundary_time": legacy_boundary,
                "ids_at_boundary": [],
                "legacy_ids_at_boundary": legacy_boundary_ids[:1_000],
            }
            bootstrap_pending = False

        items = []
        is_bootstrap = (
            not self._bootstrapped
            or bootstrap_pending
            or not bool(committed.get("initialized"))
        )
        boundary_time = str(committed.get("boundary_time") or "")
        if boundary_time:
            normalized_boundary = self._normalize_timestamp(boundary_time)
            if normalized_boundary is None:
                is_bootstrap = True
                boundary_time = ""
            else:
                boundary_time = normalized_boundary
        boundary_ids = {
            str(value) for value in committed.get("ids_at_boundary", [])
        } if isinstance(committed.get("ids_at_boundary", []), list) else set()
        legacy_boundary_ids = {
            str(value) for value in committed.get("legacy_ids_at_boundary", [])
        } if isinstance(committed.get("legacy_ids_at_boundary", []), list) else set()
        observed_times: list[str] = []
        observed_ids_by_time: dict[str, set[str]] = {}
        scan_started_at = self._utc_now_z()

        # One larger local SQLite query avoids unstable OFFSET pagination while
        # the separate sync process is inserting new messages. The product of
        # the legacy page controls remains the per-poll record budget.
        fetch_limit = min(max_messages * max_pages, 5_000)
        if is_bootstrap:
            fetch_limit = min(max_messages, fetch_limit)
        raw = self._wa([
            *global_args,
            "messages",
            "list",
            "--limit",
            str(fetch_limit),
            "--page",
            "0",
        ])
        if raw is None:
            return [], watermark
        response = self._strict_json(raw)
        if (
            not isinstance(response, dict)
            or set(response) != {"success", "data", "error"}
            or response.get("success") is not True
            or response.get("error") is not None
            or not isinstance(response.get("data"), list)
        ):
            return [], watermark
        messages = response["data"]
        if len(messages) > fetch_limit:
            print("[whatsapp] whatsapp-cli exceeded the requested limit", file=sys.stderr)
            return [], watermark
        parsed_messages: list[tuple[dict, str, str, str]] = []
        seen_keys: set[str] = set()
        previous_timestamp = ""
        for msg in messages:
            if not isinstance(msg, dict):
                return [], watermark
            msg_id = msg.get("id")
            chat_jid = msg.get("chat_jid")
            timestamp = self._normalize_timestamp(
                msg.get("timestamp"), reject_future=True
            )
            sender = msg.get("sender")
            content = msg.get("content")
            from_me = msg.get("is_from_me")
            chat_name = msg.get("chat_name", "")
            media_type = msg.get("media_type", "")
            sender_name = msg.get("sender_name", "")
            if (
                len(msg) > 50
                or not isinstance(msg_id, str)
                or not msg_id
                or len(msg_id) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in msg_id)
                or not isinstance(chat_jid, str)
                or not chat_jid
                or len(chat_jid) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in chat_jid)
                or timestamp is None
                or not isinstance(sender, str)
                or len(sender) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in sender)
                or not isinstance(content, str)
                or len(content) > 100_000
                or not isinstance(from_me, bool)
                or not isinstance(chat_name, str)
                or len(chat_name) > 500
                or any(ord(char) < 32 or ord(char) == 127 for char in chat_name)
                or not isinstance(media_type, str)
                or len(media_type) > 100
                or any(ord(char) < 32 or ord(char) == 127 for char in media_type)
                or not isinstance(sender_name, str)
                or len(sender_name) > 500
                or any(ord(char) < 32 or ord(char) == 127 for char in sender_name)
            ):
                return [], watermark
            message_key = self._message_key(chat_jid, msg_id)
            if message_key in seen_keys or (
                previous_timestamp and timestamp > previous_timestamp
            ):
                return [], watermark
            seen_keys.add(message_key)
            previous_timestamp = timestamp
            parsed_messages.append((msg, timestamp, message_key, msg_id))

        reached_boundary = is_bootstrap or len(parsed_messages) < fetch_limit
        for msg, timestamp, message_key, msg_id in parsed_messages:
            observed_times.append(timestamp)
            observed_ids_by_time.setdefault(timestamp, set()).add(message_key)
            if is_bootstrap:
                continue
            if timestamp < boundary_time:
                reached_boundary = True
                continue
            if timestamp == boundary_time and (
                message_key in boundary_ids or msg_id in legacy_boundary_ids
            ):
                # Do not stop merely because one known ID at the boundary was
                # seen. A newly synced ID at that timestamp is still new.
                continue
            if msg["is_from_me"] is True:
                continue

            chat_jid = msg["chat_jid"]
            if watched and chat_jid not in watched:
                continue
            sender = msg["sender"]
            sender_name = msg.get("sender_name", "")
            chat_name = msg.get("chat_name", "")
            content = msg["content"]
            media_type = msg.get("media_type", "")
            display_sender = sender_name or sender or chat_name

            items.append({
                "id": f"whatsapp-{message_key}",
                "source": "whatsapp",
                "type": "whatsapp_message",
                "title": f"Message from {display_sender}" if display_sender else "New WhatsApp message",
                "preview": content[:200] if content else (f"[{media_type}]" if media_type else "[empty]"),
                "discovered_at": scan_started_at,
                "author": sender,
                "author_name": sender_name,
                "group": chat_name or chat_jid,
                "url": "",
                "metadata": {
                    "msg_id": msg_id,
                    "chat_jid": chat_jid,
                    "chat_name": chat_name,
                    "timestamp": timestamp,
                    "media_type": media_type,
                },
            })

        if not reached_boundary:
            print(
                f"[whatsapp] message backlog exceeded the {fetch_limit}-record poll budget; preserving watermark",
                file=sys.stderr,
            )
            return items, watermark
        next_boundary = (
            max([boundary_time, *observed_times])
            if observed_times
            else (scan_started_at if is_bootstrap else boundary_time)
        )
        next_boundary_ids = set(boundary_ids) if next_boundary == boundary_time else set()
        next_boundary_ids.update(observed_ids_by_time.get(next_boundary, set()))
        next_legacy_ids = (
            set(legacy_boundary_ids) if next_boundary == boundary_time else set()
        )
        if len(next_boundary_ids) + len(next_legacy_ids) > 1000:
            print("[whatsapp] too many messages share one boundary timestamp", file=sys.stderr)
            return [], watermark
        candidate = {
            "initialized": True,
            "boundary_time": next_boundary,
            "ids_at_boundary": sorted(next_boundary_ids),
            "legacy_ids_at_boundary": sorted(next_legacy_ids),
        }
        self._snapshot = {
            "schema_version": 4,
            "committed": committed,
            "candidate": candidate,
            "candidate_watermark": scan_started_at,
            "bootstrap_pending": is_bootstrap,
        }
        save_snapshot("whatsapp_messages", self._snapshot)
        self._bootstrapped = True
        return items, scan_started_at

    @staticmethod
    def _normalize_timestamp(
        value: object, *, reject_future: bool = False
    ) -> str | None:
        if (
            not isinstance(value, str)
            or len(value) > 64
            or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})",
                value,
            ) is None
        ):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            parsed = parsed.astimezone(timezone.utc)
            if (
                reject_future
                and parsed > datetime.now(timezone.utc) + timedelta(minutes=5)
            ):
                return None
            return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        except (TypeError, ValueError, OverflowError):
            return None
