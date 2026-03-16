"""WhatsApp scanner — watches incoming messages via `whatsapp-cli`."""

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


class WhatsAppScanner:
    name = "whatsapp"

    def __init__(self):
        self._cli_available = None
        self._snapshot = load_snapshot("whatsapp_messages")
        self._bootstrapped = bool(self._snapshot)

    def configure(self) -> dict:
        return {
            "enabled": False,
            "watch_chats": [],
            "max_messages": 20,
        }

    def _wa(self, args: list[str], timeout: int = 15) -> str | None:
        """Run whatsapp-cli command, return stdout or None on failure."""
        try:
            result = subprocess.run(
                ["whatsapp-cli"] + args,
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                print(f"[whatsapp] whatsapp-cli error: {result.stderr[:200]}", file=sys.stderr)
                return None
            return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"[whatsapp] whatsapp-cli failed: {e}", file=sys.stderr)
            return None

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        if self._cli_available is None:
            self._cli_available = ensure_tool("whatsapp-cli")
        if not self._cli_available:
            return [], watermark

        max_messages = config.get("max_messages", 20)
        watch_chats = config.get("watch_chats", [])

        raw = self._wa(["messages", "list", "--limit", str(max_messages)])
        if raw is None:
            return [], watermark

        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            return [], watermark

        if not isinstance(messages, list):
            return [], watermark

        items = []
        is_bootstrap = not self._bootstrapped

        for msg in messages:
            msg_id = msg.get("id", "")
            timestamp = msg.get("timestamp", "")

            # Watermark filtering
            if timestamp and watermark and timestamp <= watermark:
                continue

            # Dedup via snapshot
            prev = self._snapshot.get(msg_id)
            self._snapshot[msg_id] = timestamp
            if is_bootstrap:
                continue
            if prev == timestamp:
                continue

            # Chat filtering
            chat_jid = msg.get("chat_jid", "")
            if watch_chats and chat_jid not in watch_chats:
                continue

            sender = msg.get("sender", "")
            sender_name = msg.get("sender_name", "")
            content = msg.get("content", "")
            media_type = msg.get("media_type", "")

            title = f"Message from {sender_name or sender}"
            preview = content[:200] if content else f"[{media_type}]" if media_type else "[empty]"

            items.append({
                "id": f"whatsapp-{msg_id}",
                "source": "whatsapp",
                "type": "whatsapp_message",
                "title": title,
                "preview": preview,
                "discovered_at": self._utc_now_z(),
                "author": sender,
                "author_name": sender_name,
                "group": chat_jid,
                "url": "",
                "metadata": {
                    "msg_id": msg_id,
                    "chat_jid": chat_jid,
                    "timestamp": timestamp,
                    "media_type": media_type,
                },
            })

        # Save snapshot
        save_snapshot("whatsapp_messages", self._snapshot)
        self._bootstrapped = True

        return items, self._utc_now_z()
