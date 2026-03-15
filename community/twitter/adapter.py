"""Twitter/X scanner — monitors mentions and DMs via X API v2."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone


class TwitterScanner:
    name = "twitter"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "TWITTER_BEARER_TOKEN",
            "username": "",
            "user_id": "",
            "watch_dms": True,
            "max_items": 20,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(self, path: str, params: dict, token: str) -> dict | None:
        qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        url = f"https://api.x.com/2/{path}?{qs}" if qs else f"https://api.x.com/2/{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "HiveScanner/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[twitter] API error: {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        token = os.environ.get(config.get("token_env", "TWITTER_BEARER_TOKEN"), "")
        if not token:
            return [], watermark

        user_id = config.get("user_id", "")
        if not user_id:
            return [], watermark

        max_items = config.get("max_items", 20)
        pollen = []
        had_errors = False

        # --- Mentions ---
        mention_params = {
            "max_results": max_items,
            "tweet.fields": "created_at,author_id,text",
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        if watermark:
            mention_params["start_time"] = watermark

        mentions_resp = self._api(f"users/{user_id}/mentions", mention_params, token)
        if mentions_resp is None:
            had_errors = True
        else:
            # Build author lookup from includes.users
            author_lookup = {}
            for u in mentions_resp.get("includes", {}).get("users", []):
                author_lookup[u["id"]] = u

            for tweet in mentions_resp.get("data", []):
                tweet_id = tweet["id"]
                author_id = tweet.get("author_id", "")
                author_info = author_lookup.get(author_id, {})
                author_username = author_info.get("username", "")
                author_name = author_info.get("name", "")
                text = tweet.get("text", "")

                pollen.append({
                    "id": f"twitter-mention-{tweet_id}",
                    "source": "twitter",
                    "type": "twitter_mention",
                    "title": text[:100],
                    "preview": text[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": author_username,
                    "author_name": author_name,
                    "group": "Mentions",
                    "url": f"https://x.com/{author_username}/status/{tweet_id}",
                    "metadata": {
                        "tweet_id": tweet_id,
                        "author_id": author_id,
                        "created_at": tweet.get("created_at", ""),
                    },
                })

        # --- DMs ---
        if config.get("watch_dms", True):
            dm_params = {
                "dm_event.fields": "created_at,sender_id,text",
                "max_results": max_items,
            }
            dm_resp = self._api("dm_events", dm_params, token)
            if dm_resp is None:
                had_errors = True
            else:
                for event in dm_resp.get("data", []):
                    created_at = event.get("created_at", "")
                    if watermark and created_at and created_at <= watermark:
                        continue

                    event_id = event["id"]
                    text = event.get("text", "")
                    sender_id = event.get("sender_id", "")

                    pollen.append({
                        "id": f"twitter-dm-{event_id}",
                        "source": "twitter",
                        "type": "twitter_dm",
                        "title": text[:100],
                        "preview": text[:200],
                        "discovered_at": self._utc_now_z(),
                        "author": sender_id,
                        "author_name": "",
                        "group": "DMs",
                        "url": "https://x.com/messages",
                        "metadata": {
                            "event_id": event_id,
                            "sender_id": sender_id,
                            "created_at": created_at,
                        },
                    })

        if had_errors:
            return pollen, watermark
        return pollen, self._utc_now_z()


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = TwitterScanner()
    if data["command"] == "poll":
        poll_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": poll_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
