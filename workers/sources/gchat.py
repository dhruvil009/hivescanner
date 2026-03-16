"""GChat scanner — watches Google Chat spaces and DMs via `gws` CLI."""

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


class GChatScanner:
    name = "gchat"

    def __init__(self):
        self._cli_available = None
        self._snapshot = load_snapshot("gchat_messages")
        self._bootstrapped = bool(self._snapshot)

    def configure(self) -> dict:
        return {
            "enabled": False,
            "watch_spaces": [],
            "watch_dms": True,
            "username": "",
            "max_messages": 20,
        }

    def _gws(self, args: list[str], timeout: int = 15) -> str | None:
        """Run gws CLI command, return stdout or None on failure."""
        try:
            result = subprocess.run(
                ["gws"] + args,
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                print(f"[gchat] gws error: {result.stderr[:200]}", file=sys.stderr)
                return None
            return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"[gchat] gws failed: {e}", file=sys.stderr)
            return None

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        if self._cli_available is None:
            self._cli_available = ensure_tool("gws")
        if not self._cli_available:
            return [], watermark

        watch_spaces = config.get("watch_spaces", [])
        if not watch_spaces:
            return [], watermark

        username = config.get("username", "")
        items = []
        is_bootstrap = not self._bootstrapped

        for space_id in watch_spaces:
            raw = self._gws([
                "chat", "messages", "list",
                "--parent", space_id,
                "--output", "json",
            ])
            if raw is None:
                continue

            try:
                messages = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(messages, list):
                continue

            for msg in messages:
                msg_name = msg.get("name", "")
                create_time = msg.get("createTime", "")

                # Extract message ID from name (spaces/X/messages/Y)
                msg_id = msg_name.rsplit("/", 1)[-1] if "/" in msg_name else msg_name

                # Snapshot for dedup
                self._snapshot[msg_id] = create_time

                if is_bootstrap:
                    continue

                # Watermark filtering
                if create_time and watermark and create_time <= watermark:
                    continue

                sender = msg.get("sender", {})
                text = msg.get("text", "")
                space = msg.get("space", {})
                space_type = space.get("type", "")
                annotations = msg.get("annotations", [])

                # Pollen type detection
                has_user_mention = any(
                    a.get("type") == "USER_MENTION" for a in annotations
                ) if annotations else False

                if has_user_mention or (username and f"@{username}" in text):
                    pollen_type = "gchat_mention"
                elif space_type == "DM":
                    pollen_type = "gchat_dm"
                else:
                    pollen_type = "gchat_dm"

                author_name = sender.get("displayName", "")
                author = sender.get("name", "")

                items.append({
                    "id": f"gchat-{msg_id}",
                    "source": "gchat",
                    "type": pollen_type,
                    "title": f"Message from {author_name}" if author_name else "New message",
                    "preview": text[:200] if text else "",
                    "discovered_at": self._utc_now_z(),
                    "author": author,
                    "author_name": author_name,
                    "group": "Google Chat",
                    "url": "",
                    "metadata": {
                        "message_name": msg_name,
                        "space_id": space_id,
                        "space_type": space_type,
                        "create_time": create_time,
                    },
                })

        # Save snapshot
        save_snapshot("gchat_messages", self._snapshot)
        self._bootstrapped = True

        return items, self._utc_now_z()
