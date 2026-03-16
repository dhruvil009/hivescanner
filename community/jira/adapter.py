"""Jira scanner — monitors Jira issues for assignments, updates, and mentions."""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional


class JiraScanner:
    name = "jira"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "JIRA_TOKEN",
            "domain": "",
            "username": "",
            "max_items": 20,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(self, path: str, domain: str, username: str, token: str, params: Optional[dict] = None) -> Optional[dict]:
        """Call Jira REST API v3 with Basic auth."""
        url = f"https://{domain}/rest/api/3/{path}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"
        credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[jira] API error ({path}): {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        token = os.environ.get(config.get("token_env", "JIRA_TOKEN"), "")
        domain = config.get("domain", "")
        username = config.get("username", "")
        max_items = config.get("max_items", 20)

        if not token or not domain:
            return [], watermark

        jql = (
            f'updated >= "{watermark}" '
            f"AND (assignee = currentUser() OR mentions = currentUser()) "
            f"ORDER BY updated DESC"
        )

        result = self._api("search", domain, username, token, {
            "jql": jql,
            "maxResults": str(max_items),
        })
        if not result:
            return [], watermark

        issues = result.get("issues", [])
        pollen = []

        for issue in issues:
            key = issue.get("key", "")
            fields = issue.get("fields", {})
            summary = fields.get("summary", "")
            status = (fields.get("status") or {}).get("name", "")
            priority = (fields.get("priority") or {}).get("name", "")
            issue_type = (fields.get("issuetype") or {}).get("name", "")
            assignee = fields.get("assignee") or {}
            description = fields.get("description") or ""

            # Determine pollen type
            assignee_name = assignee.get("displayName", "")
            assignee_email = assignee.get("emailAddress", "")
            if username and (username == assignee_email or username == assignee_name):
                pollen_type = "jira_assigned"
            elif username and isinstance(description, str) and username in description:
                pollen_type = "jira_mentioned"
            else:
                pollen_type = "jira_updated"

            pollen_id = f"jira-{key}"
            pollen.append({
                "id": pollen_id,
                "source": "jira",
                "type": pollen_type,
                "title": f"{key}: {summary}"[:100],
                "preview": f"[{status}] {summary}"[:200],
                "discovered_at": self._utc_now_z(),
                "author": assignee.get("emailAddress", ""),
                "author_name": assignee.get("displayName", ""),
                "group": "Issues",
                "url": f"https://{domain}/browse/{key}",
                "metadata": {
                    "issue_key": key,
                    "status": status,
                    "priority": priority,
                    "issue_type": issue_type,
                },
            })

        return pollen, self._utc_now_z()


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = JiraScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
