"""Notion scanner — monitors Notion page updates and comments."""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional


class NotionScanner:
    name = "notion"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "NOTION_TOKEN",
            "watch_databases": [],
            "watch_pages": [],
            "max_items": 20,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(self, path: str, token: str, method: str = "GET", body: Optional[dict] = None) -> Optional[dict]:
        """Call the Notion API with Bearer token auth and required version header."""
        url = f"https://api.notion.com/v1{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[notion] API error ({path}): {e}", file=sys.stderr)
            return None

    def _extract_title(self, page: dict) -> str:
        """Extract a page title from its properties."""
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                parts = prop.get("title", [])
                return "".join(t.get("plain_text", "") for t in parts)
        return ""

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        token = os.environ.get(config.get("token_env", "NOTION_TOKEN"), "")
        if not token:
            return [], watermark

        watch_databases = config.get("watch_databases", [])
        watch_pages = config.get("watch_pages", [])
        max_items = config.get("max_items", 20)

        pollen = []
        had_errors = False

        # --- Poll watched databases for recently edited pages ---
        for db_id in watch_databases:
            query_body = {
                "filter": {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"after": watermark},
                },
                "page_size": max_items,
            }
            result = self._api(f"/databases/{db_id}/query", token, method="POST", body=query_body)
            if not result:
                had_errors = True
                continue

            for page in result.get("results", []):
                page_id = page.get("id", "")
                title = self._extract_title(page)
                last_edited_by = page.get("last_edited_by", {}).get("id", "")
                page_url = page.get("url", "")

                pollen.append({
                    "id": f"notion-page-{page_id}",
                    "source": "notion",
                    "type": "notion_page_updated",
                    "title": title[:100] if title else f"Page {page_id[:8]}",
                    "preview": f"Page updated: {title}"[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": last_edited_by,
                    "author_name": "",
                    "group": f"db-{db_id[:8]}",
                    "url": page_url,
                    "metadata": {
                        "page_id": page_id,
                        "title": title,
                        "last_edited_by": last_edited_by,
                        "last_edited_time": page.get("last_edited_time", ""),
                    },
                })

        # --- Poll watched pages for updates and comments ---
        for page_id in watch_pages:
            page = self._api(f"/pages/{page_id}", token)
            if not page:
                had_errors = True
                continue

            last_edited_time = page.get("last_edited_time", "")
            if last_edited_time > watermark:
                title = self._extract_title(page)
                last_edited_by = page.get("last_edited_by", {}).get("id", "")
                page_url = page.get("url", "")

                pollen.append({
                    "id": f"notion-page-{page_id}",
                    "source": "notion",
                    "type": "notion_page_updated",
                    "title": title[:100] if title else f"Page {page_id[:8]}",
                    "preview": f"Page updated: {title}"[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": last_edited_by,
                    "author_name": "",
                    "group": "pages",
                    "url": page_url,
                    "metadata": {
                        "page_id": page_id,
                        "title": title,
                        "last_edited_by": last_edited_by,
                        "last_edited_time": last_edited_time,
                    },
                })

            # Fetch comments on watched pages
            comments_result = self._api(f"/comments?block_id={page_id}", token)
            if not comments_result:
                had_errors = True
                continue

            for comment in comments_result.get("results", []):
                comment_id = comment.get("id", "")
                created_time = comment.get("created_time", "")
                if created_time <= watermark:
                    continue

                rich_text = comment.get("rich_text", [])
                text = "".join(t.get("plain_text", "") for t in rich_text)
                created_by = comment.get("created_by", {}).get("id", "")

                pollen.append({
                    "id": f"notion-comment-{comment_id}",
                    "source": "notion",
                    "type": "notion_comment",
                    "title": text[:100] if text else "Comment",
                    "preview": text[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": created_by,
                    "author_name": "",
                    "group": f"page-{page_id[:8]}",
                    "url": page.get("url", ""),
                    "metadata": {
                        "page_id": page_id,
                        "comment_id": comment_id,
                        "title": self._extract_title(page) if page else "",
                        "last_edited_by": created_by,
                        "created_time": created_time,
                    },
                })

        if had_errors:
            return pollen, watermark
        return pollen, self._utc_now_z()


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = NotionScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
