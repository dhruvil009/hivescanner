"""PagerDuty scanner — monitors PagerDuty incidents and alerts."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional


class PagerDutyScanner:
    name = "pagerduty"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "PAGERDUTY_TOKEN",
            "user_id": "",
            "max_items": 20,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(self, path: str, token: str, params: Optional[dict] = None) -> Optional[dict]:
        """Call PagerDuty REST API v2."""
        url = f"https://api.pagerduty.com{path}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}" if "?" not in url else f"{url}&{query}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Token token={token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[pagerduty] API error ({path}): {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        token = os.environ.get(config.get("token_env", "PAGERDUTY_TOKEN"), "")
        if not token:
            return [], watermark

        user_id = config.get("user_id", "")
        max_items = config.get("max_items", 20)

        url = (
            f"/incidents?statuses[]=triggered&statuses[]=acknowledged"
            f"&since={watermark}&sort_by=created_at&limit={max_items}"
        )
        if user_id:
            url += f"&user_ids[]={user_id}"

        result = self._api(url, token)
        if not result:
            return [], watermark

        incidents = result.get("incidents", [])
        pollen = []

        for incident in incidents:
            incident_id = incident.get("id", "")
            status = incident.get("status", "")
            title = incident.get("title", "")
            urgency = incident.get("urgency", "")
            service = incident.get("service", {})
            service_name = service.get("summary", "") if service else ""
            assignee = incident.get("assignments", [{}])
            assignee_obj = assignee[0].get("assignee", {}) if assignee else {}

            if status == "triggered":
                pollen_type = "pagerduty_triggered"
            else:
                pollen_type = "pagerduty_incident"

            pollen.append({
                "id": f"pagerduty-{incident_id}",
                "source": "pagerduty",
                "type": pollen_type,
                "title": title[:100],
                "preview": f"[{urgency}] {title}"[:200],
                "discovered_at": self._utc_now_z(),
                "author": assignee_obj.get("id", ""),
                "author_name": assignee_obj.get("summary", ""),
                "group": service_name or "PagerDuty",
                "url": incident.get("html_url", ""),
                "metadata": {
                    "urgency": urgency,
                    "service_name": service_name,
                    "status": status,
                    "incident_number": incident.get("incident_number", 0),
                },
            })

        return pollen, self._utc_now_z()


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = PagerDutyScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
