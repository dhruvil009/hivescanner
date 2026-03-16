"""Discord scanner — monitors Discord DMs and channel mentions."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional, Union


class DiscordScanner:
    name = "discord"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "DISCORD_BOT_TOKEN",
            "watch_channels": [],
            "watch_dms": True,
            "user_id": "",
            "max_messages": 20,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(self, endpoint: str, token: str, params: Optional[dict] = None) -> Optional[Union[list, dict]]:
        """Call the Discord REST API v10 with Bot token auth."""
        url = f"https://discord.com/api/v10{endpoint}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[discord] API error ({endpoint}): {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        token = os.environ.get(config.get("token_env", "DISCORD_BOT_TOKEN"), "")
        if not token:
            return [], watermark

        user_id = config.get("user_id", "")
        max_messages = config.get("max_messages", 20)
        watch_channels = config.get("watch_channels", [])
        watch_dms = config.get("watch_dms", True)

        # Discord uses snowflake IDs as watermarks
        watermark_snowflake = watermark if watermark else "0"

        pollen = []
        had_errors = False
        highest_id = watermark_snowflake

        # Collect channels to poll: (channel_id, guild_id_or_none, is_dm)
        channels_to_poll = []

        for ch_id in watch_channels:
            channels_to_poll.append((ch_id, None, False))

        if watch_dms:
            dm_channels = self._api("/users/@me/channels", token)
            if dm_channels is not None and isinstance(dm_channels, list):
                for ch in dm_channels:
                    channels_to_poll.append((ch["id"], None, True))
            elif dm_channels is None:
                had_errors = True

        for channel_id, guild_id, is_dm in channels_to_poll:
            params = {"limit": str(max_messages)}
            if watermark_snowflake != "0":
                params["after"] = watermark_snowflake

            messages = self._api(f"/channels/{channel_id}/messages", token, params)
            if messages is None:
                had_errors = True
                continue
            if not isinstance(messages, list):
                had_errors = True
                continue

            for msg in messages:
                msg_id = msg.get("id", "")
                content = msg.get("content", "")
                author = msg.get("author", {})
                author_username = author.get("username", "")
                msg_guild_id = msg.get("guild_id", guild_id or "")

                # Track highest snowflake ID seen
                if msg_id > highest_id:
                    highest_id = msg_id

                # Determine pollen type
                if is_dm:
                    pollen_type = "discord_dm"
                elif user_id:
                    # Check mentions array
                    mentioned = any(
                        m.get("id") == user_id for m in msg.get("mentions", [])
                    )
                    # Check inline mention in content
                    if not mentioned and f"<@{user_id}>" in content:
                        mentioned = True
                    if mentioned:
                        pollen_type = "discord_mention"
                    else:
                        # Not relevant — skip
                        continue
                else:
                    # No user_id set and not a DM — skip
                    continue

                # Build URL
                if is_dm:
                    url = f"https://discord.com/channels/@me/{channel_id}/{msg_id}"
                else:
                    url = f"https://discord.com/channels/{msg_guild_id}/{channel_id}/{msg_id}"

                pollen_id = f"discord-{msg_id}"
                pollen.append({
                    "id": pollen_id,
                    "source": "discord",
                    "type": pollen_type,
                    "title": f"{author_username}: {content[:80]}",
                    "preview": content[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": author.get("id", ""),
                    "author_name": author_username,
                    "group": "DMs" if is_dm else channel_id,
                    "url": url,
                    "metadata": {
                        "channel_id": channel_id,
                        "guild_id": msg_guild_id,
                        "author_username": author_username,
                    },
                })

        if had_errors:
            return pollen, watermark
        return pollen, highest_id if highest_id != "0" else watermark


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = DiscordScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
