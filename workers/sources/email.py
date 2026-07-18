"""Email/Gmail scanner — watches inbox for new emails via `gws` CLI (Google Workspace)."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

# Resolve imports whether run as module or standalone
try:
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists


def _reject_duplicate_key(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _strict_json(raw: str) -> object:
    return json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_key,
        parse_constant=_reject_constant,
    )


def _valid_stage(value: object, *, allow_empty: bool = False) -> bool:
    if allow_empty and value == {}:
        return True
    if not isinstance(value, dict):
        return False
    if set(value) != {"scope", "boundary_ms", "seen_ids"}:
        return False
    scope = value.get("scope")
    boundary = value.get("boundary_ms")
    seen_ids = value.get("seen_ids")
    return (
        isinstance(scope, str)
        and re.fullmatch(r"[0-9a-f]{16}", scope) is not None
        and isinstance(boundary, int)
        and not isinstance(boundary, bool)
        and 0 <= boundary <= 32_503_680_000_000
        and isinstance(seen_ids, list)
        and len(seen_ids) <= 6_000
        and all(
            isinstance(value, str)
            and 0 < len(value) <= 256
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in seen_ids
        )
        and len(set(seen_ids)) == len(seen_ids)
    )


def _parse_email_date(s: str) -> datetime | None:
    """Parse a message date (RFC 2822 from Gmail) or watermark (ISO-8601).

    Avoids importing stdlib `email.utils` because this file is named email.py
    and shadows the stdlib package whenever workers/sources/ is on sys.path.
    """
    if not s:
        return None
    s = s.strip()
    # RFC 2822 sometimes has a "(UTC)" trailing comment; drop it.
    if "(" in s:
        s = s.split(" (", 1)[0].strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    iso = s
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _parse_scanner_timestamp(value: object) -> datetime | None:
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


class EmailScanner:
    name = "email"
    _POLL_BUDGET_SECONDS = 45

    def __init__(self):
        self._cli_available = None
        self._snapshot = load_snapshot("email_messages")
        self._bootstrapped = snapshot_exists("email_messages")

    def configure(self) -> dict:
        return {
            "enabled": False,
            "vip_senders": [],
            "query": "in:inbox",
            "max_emails": 20,
            "max_pages": 5,
            "overlap_seconds": 300,
        }

    def _gws(self, args: list[str], timeout: int = 15) -> str | None:
        """Run gws CLI command, return stdout or None on failure."""
        deadline = getattr(self, "_poll_deadline", None)
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("[email] poll time budget exhausted", file=sys.stderr)
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
                    ["gws"] + args,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout,
                    env=environment,
                )
                stdout_file.seek(0)
                raw_stdout = stdout_file.read(5_000_001)
                stderr_file.seek(0)
                raw_stderr = stderr_file.read(201)
            if result.returncode != 0:
                print(
                    f"[email] gws error: {raw_stderr.decode('utf-8', errors='replace')}",
                    file=sys.stderr,
                )
                return None
            if len(raw_stdout) > 5_000_000:
                print("[email] gws output exceeded 5 MB", file=sys.stderr)
                return None
            return raw_stdout.decode("utf-8")
        except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError, ValueError) as e:
            print(f"[email] gws failed: {e}", file=sys.stderr)
            return None

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        if not isinstance(config, dict) or not isinstance(watermark, str):
            return [], watermark
        if watermark and _parse_scanner_timestamp(watermark) is None:
            print("[email] invalid watermark; preserving it", file=sys.stderr)
            return [], watermark

        max_emails = config.get("max_emails", 20)
        max_pages = config.get("max_pages", 5)
        overlap_seconds = config.get("overlap_seconds", 300)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (max_emails, max_pages, overlap_seconds)
            )
            or not 1 <= max_emails <= 20
            or not 1 <= max_pages <= 5
            or not 60 <= overlap_seconds <= 3600
        ):
            print("[email] pagination limits are invalid", file=sys.stderr)
            return [], watermark
        query = config.get("query", "in:inbox")
        if (
            not isinstance(query, str)
            or not query
            or query != query.strip()
            or len(query) > 5000
            or any(ord(char) < 32 or ord(char) == 127 for char in query)
        ):
            print("[email] query must be a bounded, control-free string", file=sys.stderr)
            return [], watermark
        configured_vips = config.get("vip_senders", [])
        if (
            not isinstance(configured_vips, list)
            or len(configured_vips) > 500
            or not all(
                isinstance(value, str)
                and value
                and value == value.strip()
                and len(value) <= 320
                and not any(ord(char) < 32 or ord(char) == 127 for char in value)
                for value in configured_vips
            )
        ):
            print("[email] vip_senders must be a bounded string list", file=sys.stderr)
            return [], watermark
        normalized_vips = [self._sender_address(value) for value in configured_vips]
        if (
            any(
                not value
                or len(value) > 320
                or value.count("@") != 1
                or any(char.isspace() for char in value)
                for value in normalized_vips
            )
            or len(set(normalized_vips)) != len(normalized_vips)
        ):
            print("[email] vip_senders contains an invalid or duplicate address", file=sys.stderr)
            return [], watermark
        vip_senders = set(normalized_vips)

        if self._cli_available is None:
            self._cli_available = ensure_tool("gws")
        if not self._cli_available:
            return [], watermark

        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS

        scan_started_at = self._utc_now_z()
        scan_started_ms = int(
            datetime.fromisoformat(scan_started_at.replace("Z", "+00:00")).timestamp()
            * 1000
        )
        scope = hashlib.sha256(query.encode()).hexdigest()[:16]

        # Snapshot updates are staged until the scanner loop commits the
        # returned watermark. `seen_ids` lets a backlog drain in bounded chunks
        # while `boundary_ms` stays fixed until the complete list is processed.
        if (
            type(self._snapshot.get("schema_version")) is int
            and self._snapshot.get("schema_version") == 3
        ):
            committed = self._snapshot.get("committed")
            candidate = self._snapshot.get("candidate")
            candidate_wm = self._snapshot.get("candidate_watermark")
            bootstrap_pending = self._snapshot.get("bootstrap_pending")
            unstaged = candidate == {} and candidate_wm == ""
            if (
                set(self._snapshot)
                != {
                    "schema_version",
                    "committed",
                    "candidate",
                    "candidate_watermark",
                    "bootstrap_pending",
                }
                or type(self._snapshot.get("schema_version")) is not int
                or not _valid_stage(committed)
                or not _valid_stage(candidate, allow_empty=True)
                or not isinstance(candidate_wm, str)
                or not isinstance(bootstrap_pending, bool)
                or (candidate == {}) != (candidate_wm == "")
                or (not unstaged and _parse_scanner_timestamp(candidate_wm) is None)
                or (unstaged and bootstrap_pending)
            ):
                print("[email] invalid persisted snapshot; preserving watermark", file=sys.stderr)
                return [], watermark
            current_wm = _parse_scanner_timestamp(watermark) if watermark else None
            staged_wm = _parse_scanner_timestamp(candidate_wm) if candidate_wm else None
            if current_wm and staged_wm and current_wm >= staged_wm:
                committed = candidate
            if bootstrap_pending and current_wm and staged_wm and current_wm >= staged_wm:
                bootstrap_pending = False
        elif "schema_version" in self._snapshot:
            print("[email] unsupported snapshot version; preserving watermark", file=sys.stderr)
            return [], watermark
        else:
            if (
                len(self._snapshot) > 6_000
                or not all(
                    isinstance(key, str)
                    and key
                    and len(key) <= 256
                    and not any(ord(char) < 32 or ord(char) == 127 for char in key)
                    and isinstance(value, str)
                    and len(value) <= 1_000
                    for key, value in self._snapshot.items()
                )
            ):
                print("[email] invalid legacy snapshot; preserving watermark", file=sys.stderr)
                return [], watermark
            committed = {}
            bootstrap_pending = False

        same_scope = committed.get("scope") == scope
        if not same_scope:
            committed = {"scope": scope, "boundary_ms": 0, "seen_ids": []}
        is_bootstrap = not self._bootstrapped or bootstrap_pending or not same_scope
        boundary_ms = committed.get("boundary_ms", 0)
        raw_seen_ids = committed.get("seen_ids", [])
        seen_order = list(raw_seen_ids)
        seen_ids = set(seen_order)

        effective_query = query
        if is_bootstrap:
            effective_query = f"({query}) after:{scan_started_ms // 1000}"
        elif boundary_ms:
            effective_query = (
                f"({query}) after:"
                f"{max(0, boundary_ms // 1000 - overlap_seconds)}"
            )

        message_stubs: list[dict] = []
        page_token = ""
        seen_page_tokens: set[str] = set()
        listed_ids: set[str] = set()
        for page_number in range(max_pages):
            params: dict[str, object] = {
                "userId": "me",
                "q": effective_query,
                "maxResults": 500,
            }
            if page_token:
                params["pageToken"] = page_token
            raw = self._gws([
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                json.dumps(params, separators=(",", ":")),
                "--format",
                "json",
            ], timeout=30)
            if raw is None:
                return [], watermark
            try:
                page = _strict_json(raw)
            except (json.JSONDecodeError, ValueError):
                return [], watermark
            if (
                not isinstance(page, dict)
                or len(page) > 10
                or "error" in page
                or not set(page).issubset(
                    {"messages", "nextPageToken", "resultSizeEstimate"}
                )
                or (
                    "resultSizeEstimate" in page
                    and (
                        isinstance(page["resultSizeEstimate"], bool)
                        or not isinstance(page["resultSizeEstimate"], int)
                        or not 0 <= page["resultSizeEstimate"] <= 1_000_000_000
                    )
                )
            ):
                return [], watermark
            stubs = page.get("messages", [])
            if not isinstance(stubs, list) or len(stubs) > 500:
                return [], watermark
            if not all(
                isinstance(value, dict)
                and set(value).issubset({"id", "threadId"})
                and isinstance(value.get("id"), str)
                and value.get("id")
                and len(value["id"]) <= 256
                and not any(
                    ord(char) < 32 or ord(char) == 127 for char in value["id"]
                )
                and (
                    "threadId" not in value
                    or (
                        isinstance(value["threadId"], str)
                        and 0 < len(value["threadId"]) <= 256
                        and not any(
                            ord(char) < 32 or ord(char) == 127
                            for char in value["threadId"]
                        )
                    )
                )
                for value in stubs
            ):
                return [], watermark
            page_ids = [value["id"] for value in stubs]
            if listed_ids.intersection(page_ids) or len(set(page_ids)) != len(page_ids):
                return [], watermark
            listed_ids.update(page_ids)
            message_stubs.extend(stubs)
            raw_page_token = page.get("nextPageToken")
            if raw_page_token in (None, ""):
                page_token = ""
            elif (
                not isinstance(raw_page_token, str)
                or len(raw_page_token) > 2000
                or any(ord(char) < 32 or ord(char) == 127 for char in raw_page_token)
            ):
                return [], watermark
            else:
                page_token = raw_page_token
            if page_token and page_token in seen_page_tokens:
                return [], watermark
            if page_token:
                seen_page_tokens.add(page_token)
            if not page_token:
                break
            if page_number + 1 >= max_pages:
                print("[email] Gmail backlog exceeded max_pages", file=sys.stderr)
                return [], watermark

        # Gmail lists newest first. Process the oldest uncommitted IDs first so
        # a cap never advances over an undispatched middle segment.
        unprocessed_ids = []
        queued_ids: set[str] = set()
        for stub in reversed(message_stubs):
            msg_id = str(stub.get("id") or "")
            if (
                msg_id
                and len(msg_id) <= 256
                and msg_id not in seen_ids
                and msg_id not in queued_ids
            ):
                unprocessed_ids.append(msg_id)
                queued_ids.add(msg_id)
        selected_ids = unprocessed_ids[:max_emails]
        fully_drained = len(selected_ids) == len(unprocessed_ids)

        items = []
        next_seen_order = list(seen_order)
        detail_errors = False
        successful_details = 0

        for msg_id in selected_ids:
            params = {
                "userId": "me",
                "id": msg_id,
                "format": "metadata",
                "metadataHeaders": ["From", "Subject", "Date"],
            }
            raw = self._gws([
                "gmail",
                "users",
                "messages",
                "get",
                "--params",
                json.dumps(params, separators=(",", ":")),
                "--format",
                "json",
            ])
            if raw is None:
                detail_errors = True
                continue
            try:
                msg = _strict_json(raw)
            except (json.JSONDecodeError, ValueError):
                detail_errors = True
                continue
            if (
                not isinstance(msg, dict)
                or len(msg) > 30
                or "error" in msg
                or msg.get("id") != msg_id
                or not isinstance(msg.get("payload"), dict)
            ):
                detail_errors = True
                continue
            payload = msg["payload"]
            headers = payload.get("headers", [])
            if (
                not isinstance(headers, list)
                or len(headers) > 10_000
                or not all(
                    isinstance(value, dict)
                    and isinstance(value.get("name"), str)
                    and isinstance(value.get("value"), str)
                    and 0 < len(value["name"]) <= 1_000
                    and len(value["value"]) <= 1_000_000
                    for value in headers
                )
            ):
                detail_errors = True
                continue
            header_values = {
                value["name"].casefold(): value["value"]
                for value in headers
            }
            if len(header_values) != len(headers):
                detail_errors = True
                continue
            sender = header_values.get("from", "")
            subject = header_values.get("subject", "")
            date = header_values.get("date", "")
            snippet = msg.get("snippet", "")
            raw_internal_date = msg.get("internalDate")
            if (
                not isinstance(snippet, str)
                or len(snippet) > 1_000_000
                or not isinstance(raw_internal_date, str)
                or not raw_internal_date.isascii()
                or not raw_internal_date.isdigit()
                or not 1 <= len(raw_internal_date) <= 20
            ):
                detail_errors = True
                continue
            internal_date_ms = int(raw_internal_date)
            if not 0 <= internal_date_ms <= 32_503_680_000_000:
                detail_errors = True
                continue
            next_seen_order.append(msg_id)
            successful_details += 1

            # The list query already applies a one-minute overlap and
            # `unprocessed_ids` excludes committed IDs. Accept an unseen ID in
            # that overlap even when Gmail exposes it slightly late.
            if is_bootstrap:
                continue

            # Pollen type detection
            sender_address = self._sender_address(sender)
            if sender_address in vip_senders:
                pollen_type = "email_urgent"
                group = "Urgent Email"
            else:
                pollen_type = "email_new"
                group = "Email"

            items.append({
                "id": f"email-{msg_id}",
                "source": "email",
                "type": pollen_type,
                "title": f"{sender}: {subject[:80]}",
                "preview": snippet or subject,
                "discovered_at": self._utc_now_z(),
                "author": sender,
                "author_name": sender,
                "group": group,
                "url": f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
                "metadata": {
                    "message_id": msg_id,
                    "from": sender,
                    "subject": subject,
                    "date": date,
                    "internal_date_ms": internal_date_ms,
                },
            })

        if detail_errors and successful_details == 0:
            return [], watermark
        next_boundary_ms = (
            scan_started_ms
            if is_bootstrap or (fully_drained and not detail_errors)
            else boundary_ms
        )
        candidate_snapshot = {
            "scope": scope,
            "boundary_ms": next_boundary_ms,
            # Gmail IDs are opaque, not chronological. Preserve processing
            # order so trimming retains the most recently observed IDs.
            "seen_ids": next_seen_order[-6000:],
        }
        self._snapshot = {
            "schema_version": 3,
            "committed": committed,
            "candidate": candidate_snapshot,
            "candidate_watermark": scan_started_at,
            "bootstrap_pending": is_bootstrap,
        }
        save_snapshot("email_messages", self._snapshot)
        self._bootstrapped = True

        return items, scan_started_at

    @staticmethod
    def _sender_address(value: str) -> str:
        text = str(value or "").strip()
        angle = re.search(r"<([^<>\s]+@[^<>\s]+)>", text)
        if angle:
            return angle.group(1).casefold()
        bare = re.search(r"(?<![^\s<])([^\s<>]+@[^\s<>]+)(?![^\s>])", text)
        return (bare.group(1) if bare else text).strip("<>\"'").casefold()
