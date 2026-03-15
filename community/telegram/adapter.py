"""Telegram scanner — monitors Telegram messages and mentions via Bot API."""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional


class TelegramScanner:
    name = "telegram"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "TELEGRAM_BOT_TOKEN",
            "watch_chats": [],
            "max_messages": 20,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(self, method: str, token: str, params: Optional[dict] = None) -> Optional[dict]:
        """Call a Telegram Bot API method with GET request."""
        url = f"https://api.telegram.org/bot{token}/{method}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[telegram] API error ({method}): {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        token = os.environ.get(config.get("token_env", "TELEGRAM_BOT_TOKEN"), "")
        if not token:
            return [], watermark

        max_messages = config.get("max_messages", 20)
        watch_chats = config.get("watch_chats", [])

        # Watermark is the last seen update_id as a string
        last_update_id = 0
        if watermark:
            try:
                last_update_id = int(watermark)
            except (ValueError, TypeError):
                last_update_id = 0

        params = {
            "limit": str(max_messages),
            "allowed_updates": json.dumps(["message"]),
        }
        if last_update_id:
            params["offset"] = str(last_update_id + 1)

        result = self._api("getUpdates", token, params)
        if not result or not result.get("ok"):
            return [], watermark

        updates = result.get("result", [])
        if not updates:
            return [], watermark

        # Get bot info for mention detection
        me_result = self._api("getMe", token)
        bot_username = ""
        if me_result and me_result.get("ok"):
            bot_username = me_result.get("result", {}).get("username", "")

        pollen = []
        new_watermark = watermark

        for update in updates:
            update_id = update.get("update_id", 0)
            msg = update.get("message")
            if not msg:
                continue

            chat = msg.get("chat", {})
            chat_id = chat.get("id", 0)

            # Filter by watch_chats if configured
            if watch_chats and chat_id not in watch_chats:
                continue

            from_user = msg.get("from", {})
            text = msg.get("text", "")
            first_name = from_user.get("first_name", "")

            # Pollen type detection
            is_mention = False
            if bot_username and f"@{bot_username}" in text:
                is_mention = True
            if msg.get("reply_to_message"):
                is_mention = True

            pollen_type = "telegram_mention" if is_mention else "telegram_message"

            pollen_id = f"telegram-{update_id}"
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
                "group": chat.get("title", str(chat_id)),
                "url": "",
                "metadata": {
                    "chat_id": chat_id,
                    "chat_title": chat.get("title", ""),
                    "from_username": from_user.get("username", ""),
                },
            })

            # Track highest update_id as new watermark
            if update_id > last_update_id:
                last_update_id = update_id

        new_watermark = str(last_update_id) if last_update_id else watermark
        return pollen, new_watermark


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = TelegramScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
