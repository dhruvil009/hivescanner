"""Facebook scanner — monitors page notifications and Messenger messages."""

import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional


class FacebookScanner:
    name = "facebook"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "FACEBOOK_TOKEN",
            "watch_pages": [],
            "max_items": 20,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _graph(self, endpoint: str, token: str, params: Optional[dict] = None) -> Optional[dict]:
        base = f"https://graph.facebook.com/v19.0{endpoint}"
        qs = f"access_token={token}"
        if params:
            for k, v in params.items():
                qs += f"&{k}={v}"
        url = f"{base}?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "HiveScanner/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[facebook] Graph API error: {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        token = os.environ.get(config.get("token_env", "FACEBOOK_TOKEN"), "")
        if not token:
            return [], watermark

        max_items = config.get("max_items", 20)
        pollen = []
        had_errors = False

        # --- Notifications ---
        notif_data = self._graph("/me/notifications", token, {
            "include_read": "false",
            "limit": str(max_items),
        })
        if notif_data is None:
            had_errors = True
        else:
            for notif in notif_data.get("data", []):
                created = notif.get("created_time", "")
                if created and created <= watermark:
                    continue

                notif_id = notif.get("id", "")
                sender = notif.get("from", {}) or {}
                pollen.append({
                    "id": f"facebook-notif-{notif_id}",
                    "source": "facebook",
                    "type": "facebook_notification",
                    "title": notif.get("title", "")[:100],
                    "preview": notif.get("title", "")[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": sender.get("id", ""),
                    "author_name": sender.get("name", ""),
                    "group": "Notifications",
                    "url": notif.get("link", ""),
                    "metadata": {
                        "notification_id": notif_id,
                        "created_time": created,
                        "application": notif.get("application", {}).get("name", ""),
                    },
                })

        # --- Messenger conversations ---
        convo_data = self._graph("/me/conversations", token, {
            "fields": "messages.limit(5){message,from,created_time}",
        })
        if convo_data is None:
            had_errors = True
        else:
            for convo in convo_data.get("data", []):
                messages = convo.get("messages", {}).get("data", [])
                for msg in messages:
                    created = msg.get("created_time", "")
                    if created and created <= watermark:
                        continue

                    msg_text = msg.get("message", "")
                    sender = msg.get("from", {}) or {}
                    msg_hash = hashlib.sha256(
                        f"{convo.get('id', '')}:{msg_text}:{created}".encode()
                    ).hexdigest()[:12]

                    pollen.append({
                        "id": f"facebook-msg-{msg_hash}",
                        "source": "facebook",
                        "type": "facebook_message",
                        "title": f"Message from {sender.get('name', 'Unknown')}",
                        "preview": msg_text[:200],
                        "discovered_at": self._utc_now_z(),
                        "author": sender.get("id", ""),
                        "author_name": sender.get("name", ""),
                        "group": "Messenger",
                        "url": "",
                        "metadata": {
                            "conversation_id": convo.get("id", ""),
                            "created_time": created,
                        },
                    })

        if had_errors:
            return pollen, watermark
        return pollen, self._utc_now_z()


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = FacebookScanner()
    if data["command"] == "poll":
        poll_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": poll_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
