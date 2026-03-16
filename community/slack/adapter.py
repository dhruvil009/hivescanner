"""Slack scanner — monitors Slack channels and DMs for messages, mentions, and thread replies."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional


class SlackScanner:
    name = "slack"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "SLACK_TOKEN",
            "watch_channels": [],
            "watch_dms": True,
            "username": "",
            "max_messages": 20,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _ts_to_iso(self, ts: str) -> str:
        """Convert Slack timestamp (epoch.micro) to ISO 8601."""
        epoch = float(ts)
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(self, method: str, token: str, params: Optional[dict] = None) -> Optional[dict]:
        """Call a Slack Web API method with Bearer token auth."""
        url = f"https://slack.com/api/{method}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[slack] API error ({method}): {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        token = os.environ.get(config.get("token_env", "SLACK_TOKEN"), "")
        if not token:
            return [], watermark

        username = config.get("username", "")
        max_messages = config.get("max_messages", 20)
        watch_channels = config.get("watch_channels", [])
        watch_dms = config.get("watch_dms", True)

        # Convert ISO watermark to Slack epoch timestamp for oldest param
        oldest = "0"
        if watermark:
            try:
                dt = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
                oldest = str(dt.timestamp())
            except (ValueError, TypeError):
                oldest = "0"

        pollen = []
        had_errors = False

        # Collect channels to poll: configured channels + DM channels
        channels_to_poll = []  # list of (channel_id, is_dm)

        for ch in watch_channels:
            channels_to_poll.append((ch, False))

        if watch_dms:
            dm_result = self._api("conversations.list", token, {"types": "im", "limit": "100"})
            if dm_result and dm_result.get("ok"):
                for ch in dm_result.get("channels", []):
                    channels_to_poll.append((ch["id"], True))
            elif dm_result and not dm_result.get("ok"):
                print(f"[slack] conversations.list error: {dm_result.get('error')}", file=sys.stderr)
                had_errors = True

        for channel_id, is_dm in channels_to_poll:
            params = {"channel": channel_id, "limit": str(max_messages)}
            if oldest != "0":
                params["oldest"] = oldest

            result = self._api("conversations.history", token, params)
            if not result or not result.get("ok"):
                if result:
                    print(f"[slack] conversations.history error for {channel_id}: {result.get('error')}", file=sys.stderr)
                had_errors = True
                continue

            for msg in result.get("messages", []):
                ts = msg.get("ts", "")
                text = msg.get("text", "")
                user = msg.get("user", "")

                # Determine pollen type
                if is_dm:
                    pollen_type = "slack_dm"
                elif username and f"<@{username}>" in text:
                    pollen_type = "slack_mention"
                elif msg.get("thread_ts") and msg.get("thread_ts") != ts:
                    pollen_type = "slack_thread_reply"
                else:
                    # Not relevant — skip
                    continue

                pollen_id = f"slack-{channel_id}-{ts}"
                pollen.append({
                    "id": pollen_id,
                    "source": "slack",
                    "type": pollen_type,
                    "title": text[:100],
                    "preview": text[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": user,
                    "author_name": msg.get("user_profile", {}).get("real_name", "") if msg.get("user_profile") else "",
                    "group": "DMs" if is_dm else channel_id,
                    "url": f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
                    "metadata": {
                        "channel_id": channel_id,
                        "ts": ts,
                        "thread_ts": msg.get("thread_ts", ""),
                        "is_dm": is_dm,
                    },
                })

        if had_errors:
            return pollen, watermark
        return pollen, self._utc_now_z()


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = SlackScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
