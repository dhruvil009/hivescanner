"""Google Chat scanner backed by the Google Workspace CLI (``gws``)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone


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

# Resolve imports whether run as module or standalone.
try:
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists


class GChatScanner:
    name = "gchat"
    _POLL_BUDGET_SECONDS = 45

    def __init__(self):
        self._cli_available: bool | None = None
        self._snapshot = load_snapshot("gchat_messages")
        self._bootstrapped = snapshot_exists("gchat_messages")

    def configure(self) -> dict:
        return {
            "enabled": False,
            "watch_spaces": [],
            "watch_dm_spaces": [],
            "watch_dms": True,
            "user_resource": "",
            "username": "",
            "max_messages": 20,
            "max_pages": 10,
            "spaces_per_poll": 5,
        }

    def _gws(self, args: list[str], timeout: int = 20) -> str | None:
        """Run a gws command and return stdout, preserving state on failure."""
        deadline = getattr(self, "_poll_deadline", None)
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("[gchat] poll time budget exhausted", file=sys.stderr)
                return None
            timeout = min(timeout, max(0.1, remaining))
        try:
            environment = os.environ.copy()
            for name in (
                "GWS_SANITIZE_TEMPLATE",
                "GOOGLE_WORKSPACE_CLI_SANITIZE_TEMPLATE",
                "GOOGLE_WORKSPACE_CLI_SANITIZE_MODE",
                "GOOGLE_WORKSPACE_CLI_LOG",
                "GOOGLE_WORKSPACE_CLI_LOG_FILE",
            ):
                environment.pop(name, None)
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                result = subprocess.run(
                    ["gws", *args],
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout,
                    env=environment,
                )
                stdout_file.seek(0)
                raw_stdout = stdout_file.read(5_000_001)
                stderr_file.seek(0)
                raw_stderr = stderr_file.read(201)
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            print(f"[gchat] gws failed: {exc}", file=sys.stderr)
            return None
        if result.returncode != 0:
            print(
                f"[gchat] gws error: {raw_stderr.decode('utf-8', errors='replace')}",
                file=sys.stderr,
            )
            return None
        if len(raw_stdout) > 5_000_000:
            print("[gchat] gws output exceeded 5 MB", file=sys.stderr)
            return None
        try:
            return raw_stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            print(f"[gchat] gws returned invalid UTF-8: {exc}", file=sys.stderr)
            return None

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _parse_result(raw: str, collection: str) -> list[dict] | None:
        """Parse the JSON wrapper returned by gws list methods."""
        try:
            data = _strict_json(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if isinstance(data, dict):
            if "error" in data:
                return None
            values = data.get(collection, [])
        elif isinstance(data, list):
            # Retain compatibility with older gws releases and test fixtures.
            values = data
        else:
            return None
        if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
            return None
        return values

    @staticmethod
    def _parse_page(raw: str, collection: str) -> tuple[list[dict], str] | None:
        try:
            data = _strict_json(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if isinstance(data, list):
            if all(isinstance(value, dict) for value in data):
                return data, ""
            return None
        if not isinstance(data, dict) or "error" in data:
            return None
        values = data.get(collection, [])
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            return None
        raw_token = data.get("nextPageToken")
        if raw_token in (None, ""):
            token = ""
        elif (
            not isinstance(raw_token, str)
            or len(raw_token) > 2000
            or any(ord(char) < 32 or ord(char) == 127 for char in raw_token)
        ):
            return None
        else:
            token = raw_token
        return values, token

    @staticmethod
    def _space_name(value: object) -> str:
        if not isinstance(value, str):
            return ""
        name = str(value or "").strip().rstrip("/")
        if name and not name.startswith("spaces/"):
            name = f"spaces/{name}"
        return name

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
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _valid_space_times(cls, value: object) -> dict[str, str] | None:
        if not isinstance(value, dict) or len(value) > 10_000:
            return None
        result: dict[str, str] = {}
        for key, timestamp in value.items():
            if (
                not isinstance(key, str)
                or re.fullmatch(r"spaces/[A-Za-z0-9_-]+", key) is None
                or cls._parse_timestamp(timestamp) is None
            ):
                return None
            result[key] = timestamp
        return result

    @classmethod
    def _valid_message_times(cls, value: object) -> dict[str, str] | None:
        if not isinstance(value, dict) or len(value) > 5_000:
            return None
        result: dict[str, str] = {}
        for key, timestamp in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 768
                or any(ord(char) < 32 or ord(char) == 127 for char in key)
                or cls._parse_timestamp(timestamp) is None
            ):
                return None
            result[key] = timestamp
        return result

    @classmethod
    def _candidate_committed(cls, watermark: str, candidate_watermark: str) -> bool:
        current = cls._parse_timestamp(watermark)
        candidate = cls._parse_timestamp(candidate_watermark)
        return current is not None and candidate is not None and current >= candidate

    @staticmethod
    def _valid_index(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
            return None
        return value

    def _discover_dm_spaces(self, max_pages: int) -> tuple[dict[str, str], bool]:
        result: dict[str, str] = {}
        page_token = ""
        seen_page_tokens: set[str] = set()
        for page_number in range(max_pages):
            params: dict[str, object] = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            raw = self._gws([
                "chat",
                "spaces",
                "list",
                "--params",
                json.dumps(params, separators=(",", ":")),
                "--format",
                "json",
            ])
            if raw is None:
                return {}, False
            parsed = self._parse_page(raw, "spaces")
            if parsed is None:
                return {}, False
            spaces, page_token = parsed
            if len(spaces) > 1_000:
                return {}, False
            for space in spaces:
                raw_name = space.get("name")
                raw_type = space.get("spaceType", space.get("type"))
                if not isinstance(raw_name, str) or not isinstance(raw_type, str):
                    return {}, False
                name = self._space_name(space.get("name"))
                space_type = raw_type
                if not name or not re.fullmatch(r"spaces/[A-Za-z0-9_-]+", name):
                    return {}, False
                if name and space_type in {"DIRECT_MESSAGE", "DM"}:
                    result[name] = space_type
            if not page_token:
                return result, True
            if page_token in seen_page_tokens:
                print("[gchat] space discovery repeated a page token", file=sys.stderr)
                return {}, False
            seen_page_tokens.add(page_token)
            if page_number + 1 >= max_pages:
                print("[gchat] space discovery exceeded max_pages", file=sys.stderr)
                return {}, False
        return result, True

    @staticmethod
    def _mentions_user(message: dict, user_resource: str, username: str) -> bool:
        annotations = message.get("annotations")
        if not isinstance(annotations, list):
            annotations = []
        for annotation in annotations:
            if not isinstance(annotation, dict) or annotation.get("type") != "USER_MENTION":
                continue
            user_mention = annotation.get("userMention")
            if not isinstance(user_mention, dict):
                continue
            user = user_mention.get("user")
            if not isinstance(user, dict):
                continue
            mentioned = user.get("name", "")
            # Without an identity, an annotation could target somebody else.
            if user_resource and mentioned == user_resource:
                return True
        if username:
            text = message.get("text") or ""
            return re.search(
                rf"(?<![\w@])@{re.escape(username)}\b", text, re.I
            ) is not None
        return False

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        if not isinstance(config, dict) or not isinstance(watermark, str):
            return [], watermark
        if watermark and self._parse_timestamp(watermark) is None:
            print("[gchat] invalid watermark; preserving it", file=sys.stderr)
            return [], watermark

        if self._snapshot.get("schema_version") == 3:
            required_snapshot_keys = {
                "schema_version",
                "committed",
                "candidate",
                "candidate_watermark",
                "bootstrap_pending",
                "dm_spaces",
                "dm_refreshed_at",
            }
            optional_snapshot_keys = {
                "committed_space_times",
                "candidate_space_times",
                "committed_next_space_index",
                "candidate_next_space_index",
            }
            snapshot_keys = set(self._snapshot)
            has_space_time_state = {
                "committed_space_times",
                "candidate_space_times",
            }.issubset(snapshot_keys)
            has_index_state = {
                "committed_next_space_index",
                "candidate_next_space_index",
            }.issubset(snapshot_keys)
            committed_messages = self._valid_message_times(
                self._snapshot.get("committed")
            )
            candidate_messages = self._valid_message_times(
                self._snapshot.get("candidate")
            )
            committed_space_times = self._valid_space_times(
                self._snapshot.get("committed_space_times", {})
            )
            candidate_space_times = self._valid_space_times(
                self._snapshot.get("candidate_space_times", {})
            )
            committed_next_index = self._valid_index(
                self._snapshot.get("committed_next_space_index", 0)
            )
            candidate_next_index = self._valid_index(
                self._snapshot.get("candidate_next_space_index", committed_next_index)
            )
            bootstrap_value = self._snapshot.get("bootstrap_pending")
            candidate_value = self._snapshot.get("candidate_watermark")
            dm_values = self._snapshot.get("dm_spaces")
            dm_refreshed_value = self._snapshot.get("dm_refreshed_at")
            if any(value is None for value in (
                committed_messages,
                candidate_messages,
                committed_space_times,
                candidate_space_times,
                committed_next_index,
                candidate_next_index,
            )) or (
                type(self._snapshot.get("schema_version")) is not int
                or not required_snapshot_keys.issubset(snapshot_keys)
                or not snapshot_keys.issubset(
                    required_snapshot_keys | optional_snapshot_keys
                )
                or has_space_time_state
                != bool(
                    snapshot_keys
                    & {"committed_space_times", "candidate_space_times"}
                )
                or has_index_state
                != bool(
                    snapshot_keys
                    & {
                        "committed_next_space_index",
                        "candidate_next_space_index",
                    }
                )
                or not isinstance(bootstrap_value, bool)
                or not isinstance(candidate_value, str)
                or (
                    candidate_value
                    and self._parse_timestamp(candidate_value) is None
                )
                or (bootstrap_value and not candidate_value)
                or not isinstance(dm_values, list)
                or len(dm_values) > 10_000
                or not all(
                    isinstance(value, str)
                    and re.fullmatch(r"spaces/[A-Za-z0-9_-]+", value)
                    for value in dm_values
                )
                or len(set(dm_values)) != len(dm_values)
                or not isinstance(dm_refreshed_value, str)
                or (
                    dm_refreshed_value
                    and self._parse_timestamp(dm_refreshed_value) is None
                )
            ):
                print("[gchat] invalid persisted snapshot; preserving watermark", file=sys.stderr)
                return [], watermark
            bootstrap_pending = bootstrap_value
            candidate_wm = candidate_value
            if (
                candidate_wm
                and self._candidate_committed(watermark, candidate_wm)
            ):
                committed_messages = candidate_messages
                committed_space_times = candidate_space_times
                committed_next_index = candidate_next_index
            if (
                bootstrap_pending
                and candidate_wm
                and self._candidate_committed(watermark, candidate_wm)
            ):
                bootstrap_pending = False
        elif self._bootstrapped:
            # Upgrade the old mixed staged/committed snapshot with one quiet
            # bootstrap so an uncommitted local write cannot suppress pollen.
            if (
                "schema_version" in self._snapshot
                or self._valid_message_times(self._snapshot) is None
            ):
                print("[gchat] invalid legacy snapshot; preserving watermark", file=sys.stderr)
                return [], watermark
            committed_messages = {}
            committed_space_times = {}
            committed_next_index = 0
            has_space_time_state = False
            bootstrap_pending = True
        else:
            if self._snapshot:
                print("[gchat] unexpected uninitialized snapshot", file=sys.stderr)
                return [], watermark
            committed_messages = {}
            committed_space_times = {}
            committed_next_index = 0
            has_space_time_state = False
            bootstrap_pending = False
        is_bootstrap = not self._bootstrapped or bootstrap_pending
        candidate_messages = dict(committed_messages)
        candidate_space_times = dict(committed_space_times)

        configured = config.get("watch_spaces", [])
        if not isinstance(configured, list):
            print("[gchat] watch_spaces must be a list", file=sys.stderr)
            return [], watermark
        configured_dms = config.get("watch_dm_spaces", [])
        if not isinstance(configured_dms, list):
            print("[gchat] watch_dm_spaces must be a list", file=sys.stderr)
            return [], watermark
        if len(configured) + len(configured_dms) > 50 or not all(
            isinstance(value, str) for value in [*configured, *configured_dms]
        ):
            print("[gchat] watch lists may contain at most 50 space names", file=sys.stderr)
            return [], watermark

        max_messages = config.get("max_messages", 20)
        max_pages = config.get("max_pages", 10)
        spaces_per_poll = config.get("spaces_per_poll", 5)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (max_messages, max_pages, spaces_per_poll)
            )
            or not 1 <= max_messages <= 1000
            or not 1 <= max_pages <= 10
            or not 1 <= spaces_per_poll <= 20
        ):
            print("[gchat] pagination limits are invalid", file=sys.stderr)
            return [], watermark

        normalized_spaces = [self._space_name(value) for value in configured]
        normalized_dms = [self._space_name(value) for value in configured_dms]
        if (
            any(
                not name
                or len(name) > 256
                or not re.fullmatch(r"spaces/[A-Za-z0-9_-]+", name)
                for name in [*normalized_spaces, *normalized_dms]
            )
            or len(set(normalized_spaces)) != len(normalized_spaces)
            or len(set(normalized_dms)) != len(normalized_dms)
            or not set(normalized_spaces).isdisjoint(normalized_dms)
        ):
            print("[gchat] Google Chat space names must be valid and unique", file=sys.stderr)
            return [], watermark
        spaces: dict[str, str] = {name: "" for name in normalized_spaces}
        spaces.update({name: "DIRECT_MESSAGE" for name in normalized_dms})
        watch_dms = config.get("watch_dms", True)
        if not isinstance(watch_dms, bool):
            print("[gchat] watch_dms must be a boolean", file=sys.stderr)
            return [], watermark

        raw_user_resource = config.get("user_resource", "")
        raw_username = config.get("username", "")
        if not isinstance(raw_user_resource, str) or not isinstance(raw_username, str):
            print("[gchat] user_resource and username must be strings", file=sys.stderr)
            return [], watermark
        user_resource = raw_user_resource
        username = raw_username[1:] if raw_username.startswith("@") else raw_username
        if user_resource and (
            len(user_resource) > 256
            or re.fullmatch(r"users/[A-Za-z0-9_-]+", user_resource) is None
        ):
            print("[gchat] invalid user_resource", file=sys.stderr)
            return [], watermark
        if username and (
            len(username) > 128
            or "@" in username
            or any(
                char.isspace() or ord(char) < 32 or ord(char) == 127
                for char in username
            )
        ):
            print("[gchat] invalid username", file=sys.stderr)
            return [], watermark

        if self._cli_available is None:
            self._cli_available = ensure_tool("gws")
        if not self._cli_available:
            return [], watermark

        discovery_ok = True
        cached_values = (
            self._snapshot.get("dm_spaces", [])
            if self._snapshot.get("schema_version") == 3
            else []
        )
        cached_dm_spaces = {
            value: "DIRECT_MESSAGE"
            for value in cached_values
        }
        dm_refreshed_at = (
            self._snapshot.get("dm_refreshed_at", "")
            if self._snapshot.get("schema_version") == 3
            else ""
        )
        if watch_dms:
            refresh_dms = not cached_dm_spaces
            if dm_refreshed_at:
                refreshed = self._parse_timestamp(dm_refreshed_at)
                if refreshed is not None:
                    refresh_dms = refresh_dms or (
                        datetime.now(timezone.utc) - refreshed
                    ).total_seconds() >= 86400
                else:
                    refresh_dms = True
            else:
                refresh_dms = True
            if refresh_dms:
                dm_spaces, discovery_ok = self._discover_dm_spaces(max_pages)
                if discovery_ok:
                    cached_dm_spaces = dm_spaces
                    dm_refreshed_at = self._utc_now_z()
                elif cached_dm_spaces:
                    # A stale cache is safer than dropping all known DMs on a
                    # transient discovery failure.
                    discovery_ok = True
                    dm_spaces = cached_dm_spaces
            else:
                dm_spaces = cached_dm_spaces
            spaces.update(dm_spaces)
            if len(spaces) > 10_000:
                print(
                    "[gchat] more than 10,000 spaces discovered; narrow the watched spaces",
                    file=sys.stderr,
                )
                return [], watermark
            if any(
                len(name) > 256
                or not re.fullmatch(r"spaces/[A-Za-z0-9_-]+", name)
                for name in spaces
            ):
                print("[gchat] discovered an invalid Google Chat space name", file=sys.stderr)
                return [], watermark
        if not spaces:
            # An empty successful discovery is still a valid bootstrap.
            if discovery_ok:
                empty_watermark = watermark or self._utc_now_z()
                self._snapshot = {
                    "schema_version": 3,
                    "committed": committed_messages,
                    "candidate": candidate_messages,
                    "committed_space_times": committed_space_times,
                    "candidate_space_times": candidate_space_times,
                    "committed_next_space_index": committed_next_index,
                    "candidate_next_space_index": 0,
                    "candidate_watermark": empty_watermark,
                    "bootstrap_pending": is_bootstrap,
                    "dm_spaces": sorted(cached_dm_spaces),
                    "dm_refreshed_at": dm_refreshed_at,
                }
                save_snapshot("gchat_messages", self._snapshot)
                self._bootstrapped = True
                return [], empty_watermark
            return [], watermark

        ordered_spaces = sorted(spaces)
        active_spaces = set(ordered_spaces)
        committed_space_times = {
            key: value
            for key, value in committed_space_times.items()
            if key in active_spaces
        }
        # Schema-3 snapshots created before per-space boundaries used the main
        # watermark. Seed those once so upgrading does not silently bootstrap
        # away messages that were not in the old overlap snapshot.
        if (
            not has_space_time_state
            and not is_bootstrap
            and self._parse_timestamp(watermark) is not None
        ):
            committed_space_times = {
                space_name: watermark for space_name in ordered_spaces
            }
        candidate_space_times = dict(committed_space_times)
        start_index = committed_next_index % len(ordered_spaces)
        selected_count = min(spaces_per_poll, len(ordered_spaces))
        selected_spaces = [
            ordered_spaces[(start_index + offset) % len(ordered_spaces)]
            for offset in range(selected_count)
        ]
        candidate_next_index = (start_index + selected_count) % len(ordered_spaces)

        items: list[dict] = []
        successful_spaces = 0
        scan_started_at = self._utc_now_z()

        for space_name in selected_spaces:
            discovered_type = spaces[space_name]
            space_boundary = committed_space_times.get(space_name, "")
            space_bootstrap = is_bootstrap or not space_boundary
            overlap_boundary = ""
            if not space_bootstrap:
                parsed_boundary = self._parse_timestamp(space_boundary)
                if parsed_boundary is None:
                    print(f"[gchat] invalid boundary for {space_name}", file=sys.stderr)
                    continue
                overlap_boundary = (
                    parsed_boundary - timedelta(minutes=5)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            base_params: dict[str, object] = {
                "parent": space_name,
                "pageSize": max_messages,
                "orderBy": "createTime desc",
            }
            if overlap_boundary:
                base_params["filter"] = f'createTime > "{overlap_boundary}"'
            messages: list[dict] = []
            page_token = ""
            seen_page_tokens: set[str] = set()
            pages_to_fetch = 1 if space_bootstrap else max_pages
            space_ok = True
            for page_number in range(pages_to_fetch):
                params = dict(base_params)
                if page_token:
                    params["pageToken"] = page_token
                raw = self._gws([
                    "chat",
                    "spaces",
                    "messages",
                    "list",
                    "--params",
                    json.dumps(params, separators=(",", ":")),
                    "--format",
                    "json",
                ])
                if raw is None:
                    space_ok = False
                    break
                parsed = self._parse_page(raw, "messages")
                if parsed is None:
                    space_ok = False
                    break
                page_messages, page_token = parsed
                if (
                    len(page_messages) > max_messages
                    or len(messages) + len(page_messages) > max_messages * pages_to_fetch
                ):
                    print(f"[gchat] oversized message page for {space_name}", file=sys.stderr)
                    space_ok = False
                    break
                messages.extend(page_messages)
                if not page_token:
                    break
                if page_token in seen_page_tokens:
                    print(f"[gchat] repeated page token for {space_name}", file=sys.stderr)
                    space_ok = False
                    break
                seen_page_tokens.add(page_token)
                if page_number + 1 >= pages_to_fetch and not space_bootstrap:
                    print(
                        f"[gchat] message backlog for {space_name} exceeded max_pages",
                        file=sys.stderr,
                    )
                    space_ok = False
            if not space_ok:
                continue

            space_updates: dict[str, str] = {}
            space_items: list[dict] = []
            previous_create_time: datetime | None = None
            for msg in messages:
                msg_name = msg.get("name")
                create_time = msg.get("createTime")
                parsed_create_time = self._parse_timestamp(create_time)
                prefix = f"{space_name}/messages/"
                if (
                    not isinstance(msg_name, str)
                    or not msg_name.startswith(prefix)
                    or len(msg_name) > 768
                    or any(ord(char) < 32 or ord(char) == 127 for char in msg_name)
                    or parsed_create_time is None
                    or (
                        previous_create_time is not None
                        and parsed_create_time > previous_create_time
                    )
                ):
                    space_ok = False
                    break
                previous_create_time = parsed_create_time
                msg_id = msg_name.rsplit("/", 1)[-1] if msg_name else ""
                if (
                    not msg_id
                    or len(msg_id) > 256
                    or "/" in msg_id
                    or msg_name in space_updates
                ):
                    space_ok = False
                    break
                sender_value = msg.get("sender")
                if sender_value is None:
                    sender = {}
                elif isinstance(sender_value, dict):
                    sender = sender_value
                else:
                    space_ok = False
                    break
                author_value = sender.get("name", "")
                author_name_value = sender.get("displayName", "")
                text_value = msg.get("text")
                formatted_value = msg.get("formattedText")
                embedded_value = msg.get("space")
                annotations = msg.get("annotations", [])
                if (
                    not isinstance(author_value, str)
                    or len(author_value) > 256
                    or (
                        author_value
                        and re.fullmatch(r"users/[A-Za-z0-9_-]+", author_value) is None
                    )
                    or not isinstance(author_name_value, str)
                    or len(author_name_value) > 10_000
                    or (text_value is not None and not isinstance(text_value, str))
                    or (isinstance(text_value, str) and len(text_value) > 100_000)
                    or (formatted_value is not None and not isinstance(formatted_value, str))
                    or (
                        isinstance(formatted_value, str)
                        and len(formatted_value) > 100_000
                    )
                    or (embedded_value is not None and not isinstance(embedded_value, dict))
                    or not isinstance(annotations, list)
                    or len(annotations) > 10_000
                    or not all(isinstance(annotation, dict) for annotation in annotations)
                ):
                    space_ok = False
                    break
                for annotation in annotations:
                    annotation_type = annotation.get("type")
                    if (
                        not isinstance(annotation_type, str)
                        or len(annotation_type) > 128
                    ):
                        space_ok = False
                        break
                    if annotation_type == "USER_MENTION":
                        mention = annotation.get("userMention")
                        mentioned_user = (
                            mention.get("user") if isinstance(mention, dict) else None
                        )
                        mentioned_name = (
                            mentioned_user.get("name")
                            if isinstance(mentioned_user, dict)
                            else None
                        )
                        if (
                            not isinstance(mentioned_name, str)
                            or re.fullmatch(
                                r"users/[A-Za-z0-9_-]+", mentioned_name
                            )
                            is None
                        ):
                            space_ok = False
                            break
                if not space_ok:
                    break

                snapshot_key = msg_name
                space_updates[snapshot_key] = create_time
                if space_bootstrap or snapshot_key in committed_messages:
                    continue

                author = author_value
                if user_resource and author == user_resource:
                    continue
                text = text_value or formatted_value or ""
                embedded_space = embedded_value or {}
                raw_space_type = (
                    embedded_space.get("spaceType")
                    or embedded_space.get("type")
                    or discovered_type
                    or ""
                )
                if not isinstance(raw_space_type, str):
                    space_ok = False
                    break
                space_type = raw_space_type
                is_dm = space_type in {"DIRECT_MESSAGE", "DM"}
                mentioned = self._mentions_user(msg, user_resource, username)
                # Group-space chatter is noise unless it mentions this user.
                if not is_dm and not mentioned:
                    continue

                author_name = author_name_value
                space_items.append({
                    "id": (
                        f"gchat-{hashlib.sha256(space_name.encode()).hexdigest()[:12]}-"
                        f"{msg_id}"
                    ),
                    "source": "gchat",
                    "type": "gchat_mention" if mentioned else "gchat_dm",
                    "title": f"Message from {author_name or author}" if (author_name or author) else "New message",
                    "preview": text[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": author,
                    "author_name": author_name,
                    "group": space_name,
                    "url": "",
                    "metadata": {
                        "message_name": msg_name,
                        "space_id": space_name,
                        "space_type": space_type,
                        "create_time": create_time,
                    },
                })

            if not space_ok:
                continue
            candidate_messages.update(space_updates)
            candidate_space_times[space_name] = scan_started_at
            items.extend(space_items)
            successful_spaces += 1

        # Bound long-running state while retaining ample overlap for retries.
        retention_cutoff = (
            datetime.fromisoformat(scan_started_at.replace("Z", "+00:00"))
            - timedelta(minutes=10)
        )
        recent_messages = {}
        for key, value in candidate_messages.items():
            try:
                created = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created.astimezone(timezone.utc) >= retention_cutoff:
                    recent_messages[key] = value
            except (TypeError, ValueError, OverflowError):
                continue
        candidate_messages = recent_messages
        if len(candidate_messages) > 5_000:
            print("[gchat] overlap state exceeded 5,000 messages", file=sys.stderr)
            return [], watermark

        # If every configured space was attempted and every attempt failed,
        # there is neither a checkpoint nor scheduler progress to commit.
        if successful_spaces == 0 and candidate_next_index == committed_next_index:
            return [], watermark

        self._snapshot = {
            "schema_version": 3,
            "committed": committed_messages,
            "candidate": candidate_messages,
            "committed_space_times": committed_space_times,
            "candidate_space_times": candidate_space_times,
            "committed_next_space_index": committed_next_index,
            "candidate_next_space_index": candidate_next_index,
            "candidate_watermark": scan_started_at,
            "bootstrap_pending": is_bootstrap,
            "dm_spaces": sorted(cached_dm_spaces),
            "dm_refreshed_at": dm_refreshed_at,
        }
        save_snapshot("gchat_messages", self._snapshot)
        self._bootstrapped = True
        return items, scan_started_at
