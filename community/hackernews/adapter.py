"""Hacker News scanner — monitors top stories and username mentions."""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone


class HackerNewsScanner:
    name = "hackernews"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "watch_keywords": [],
            "username": "",
            "min_points": 100,
            "max_items": 20,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _watermark_to_epoch(self, watermark: str) -> int:
        """Convert ISO watermark string to epoch seconds."""
        try:
            dt = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except Exception:
            return 0

    def _api_get(self, path: str) -> dict | None:
        url = f"https://hn.algolia.com/api/v1{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "HiveScanner/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[hackernews] API error: {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        keywords = config.get("watch_keywords", [])
        username = config.get("username", "").strip()
        min_points = config.get("min_points", 100)
        max_items = config.get("max_items", 20)
        epoch = self._watermark_to_epoch(watermark)

        pollen = []
        had_errors = False
        seen_ids: set[str] = set()

        # Query 1: Top stories matching keywords
        for keyword in keywords:
            encoded_kw = urllib.parse.quote(keyword)
            path = (
                f"/search?tags=story"
                f"&query={encoded_kw}"
                f"&numericFilters=points>{min_points},created_at_i>{epoch}"
            )
            result = self._api_get(path)
            if result is None:
                had_errors = True
                continue

            for hit in result.get("hits", [])[:max_items]:
                obj_id = hit.get("objectID", "")
                pollen_id = f"hn-story-{obj_id}"
                if pollen_id in seen_ids:
                    continue
                seen_ids.add(pollen_id)

                pollen.append({
                    "id": pollen_id,
                    "source": "hackernews",
                    "type": "hn_top_story",
                    "title": (hit.get("title") or "")[:100],
                    "preview": (hit.get("title") or "")[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": hit.get("author", ""),
                    "author_name": hit.get("author", ""),
                    "group": "Hacker News",
                    "url": f"https://news.ycombinator.com/item?id={obj_id}",
                    "metadata": {
                        "points": hit.get("points", 0),
                        "num_comments": hit.get("num_comments", 0),
                        "author": hit.get("author", ""),
                        "keyword": keyword,
                    },
                })

        # Query 2: Mentions of username
        if username:
            encoded_user = urllib.parse.quote(username)
            path = (
                f"/search?query={encoded_user}"
                f"&numericFilters=created_at_i>{epoch}"
            )
            result = self._api_get(path)
            if result is None:
                had_errors = True
            else:
                for hit in result.get("hits", [])[:max_items]:
                    obj_id = hit.get("objectID", "")
                    pollen_id = f"hn-mention-{obj_id}"
                    if pollen_id in seen_ids:
                        continue
                    seen_ids.add(pollen_id)

                    title = hit.get("title") or ""
                    comment_text = hit.get("comment_text") or ""
                    display_title = title if title else comment_text[:80]

                    pollen.append({
                        "id": pollen_id,
                        "source": "hackernews",
                        "type": "hn_mention",
                        "title": display_title[:100],
                        "preview": display_title[:200],
                        "discovered_at": self._utc_now_z(),
                        "author": hit.get("author", ""),
                        "author_name": hit.get("author", ""),
                        "group": "Hacker News",
                        "url": f"https://news.ycombinator.com/item?id={obj_id}",
                        "metadata": {
                            "points": hit.get("points", 0),
                            "num_comments": hit.get("num_comments", 0),
                            "author": hit.get("author", ""),
                        },
                    })

        if had_errors:
            return pollen, watermark
        return pollen, self._utc_now_z()


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = HackerNewsScanner()
    if data["command"] == "poll":
        poll_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": poll_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
