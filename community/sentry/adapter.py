"""Sentry scanner — monitors Sentry issues and error spikes."""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional, Union


class SentryScanner:
    name = "sentry"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "SENTRY_TOKEN",
            "organization": "",
            "project": "",
            "max_items": 20,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(self, path: str, token: str, params: Optional[dict] = None) -> Optional[Union[list, dict]]:
        """Call the Sentry REST API with Bearer token auth."""
        url = f"https://sentry.io/api/0{path}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}" if "?" not in url else f"{url}&{query}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[sentry] API error ({path}): {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        token = os.environ.get(config.get("token_env", "SENTRY_TOKEN"), "")
        if not token:
            return [], watermark

        organization = config.get("organization", "")
        if not organization:
            return [], watermark

        project = config.get("project", "")
        max_items = config.get("max_items", 20)

        # Build the appropriate endpoint
        if project:
            path = f"/projects/{organization}/{project}/issues/"
        else:
            path = f"/organizations/{organization}/issues/"

        params = {
            "query": "is:unresolved",
            "sort": "date",
            "limit": str(max_items),
        }

        issues = self._api(path, token, params)
        if not issues or not isinstance(issues, list):
            return [], watermark

        pollen = []
        new_watermark = watermark

        for issue in issues:
            last_seen = issue.get("lastSeen", "")

            # Watermark filtering on lastSeen
            if watermark and last_seen and last_seen <= watermark:
                continue

            # Track highest lastSeen as new watermark
            if last_seen and (not new_watermark or last_seen > new_watermark):
                new_watermark = last_seen

            issue_id = issue.get("id", "")
            title = issue.get("title", "")
            level = issue.get("level", "")
            platform = issue.get("platform", "")
            event_count = int(issue.get("count", "0"))
            permalink = issue.get("permalink", "")
            short_id = issue.get("shortId", "")
            is_subscribed = issue.get("isSubscribed", False)

            # Determine pollen type
            if is_subscribed or event_count > 100:
                pollen_type = "sentry_spike"
            else:
                pollen_type = "sentry_issue"

            pollen_id = f"sentry-{issue_id}"
            pollen.append({
                "id": pollen_id,
                "source": "sentry",
                "type": pollen_type,
                "title": f"{short_id}: {title}"[:100] if short_id else title[:100],
                "preview": f"[{level}] {title}"[:200],
                "discovered_at": self._utc_now_z(),
                "author": "",
                "author_name": "",
                "group": project or organization,
                "url": permalink,
                "metadata": {
                    "issue_id": issue_id,
                    "level": level,
                    "platform": platform,
                    "count": event_count,
                    "last_seen": last_seen,
                },
            })

        return pollen, new_watermark if new_watermark else watermark


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = SentryScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
