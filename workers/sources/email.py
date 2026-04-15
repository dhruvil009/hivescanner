"""Email/Gmail scanner — watches inbox for new emails via `gws` CLI (Google Workspace)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

# Resolve imports whether run as module or standalone
try:
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot
except ImportError:
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot


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


class EmailScanner:
    name = "email"

    def __init__(self):
        self._cli_available = None
        self._snapshot = load_snapshot("email_messages")
        self._bootstrapped = bool(self._snapshot)

    def configure(self) -> dict:
        return {
            "enabled": False,
            "vip_senders": [],
            "max_emails": 20,
        }

    def _gws(self, args: list[str], timeout: int = 15) -> str | None:
        """Run gws CLI command, return stdout or None on failure."""
        try:
            result = subprocess.run(
                ["gws"] + args,
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                print(f"[email] gws error: {result.stderr[:200]}", file=sys.stderr)
                return None
            return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"[email] gws failed: {e}", file=sys.stderr)
            return None

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        if self._cli_available is None:
            self._cli_available = ensure_tool("gws")
        if not self._cli_available:
            return [], watermark

        raw = self._gws(["gmail", "+triage", "--format", "json"])
        if raw is None:
            return [], watermark

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return [], watermark

        # gws returns {"messages": [...]} wrapper, not a raw list
        if isinstance(data, dict):
            emails = data.get("messages", [])
        elif isinstance(data, list):
            emails = data
        else:
            return [], watermark

        max_emails = config.get("max_emails", 20)
        emails = emails[:max_emails]

        is_bootstrap = not self._bootstrapped
        vip_senders = [s.lower() for s in config.get("vip_senders", [])]
        items = []

        for msg in emails:
            msg_id = msg.get("id", "")
            if not msg_id:
                continue

            sender = msg.get("from", "")
            subject = msg.get("subject", "")
            date = msg.get("date", "")
            snippet = msg.get("snippet", "")

            # Snapshot for dedup
            self._snapshot[msg_id] = date

            if is_bootstrap:
                continue

            # Watermark filtering — Gmail sends RFC 2822, watermark is ISO-8601.
            # Lexicographic comparison between the two formats is meaningless,
            # so parse both to datetimes before comparing.
            if date and watermark:
                msg_dt = _parse_email_date(date)
                wm_dt = _parse_email_date(watermark)
                if msg_dt and wm_dt and msg_dt <= wm_dt:
                    continue

            # Pollen type detection
            sender_lower = sender.lower()
            if any(vip in sender_lower for vip in vip_senders):
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
                "url": "",
                "metadata": {
                    "message_id": msg_id,
                    "from": sender,
                    "subject": subject,
                    "date": date,
                },
            })

        # Save snapshot
        save_snapshot("email_messages", self._snapshot)
        self._bootstrapped = True

        return items, self._utc_now_z()
