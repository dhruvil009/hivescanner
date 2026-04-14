"""Linear scanner — monitors Linear issues and status changes."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Inlined snapshot_store to keep adapter self-contained when sandboxed.
_HIVESCANNER_HOME = Path.home() / ".hivescanner"
_SNAPSHOTS_FILE = _HIVESCANNER_HOME / "snapshots.json"


def _load_all() -> dict:
    if not _SNAPSHOTS_FILE.exists():
        return {}
    try:
        return json.loads(_SNAPSHOTS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all(data: dict) -> None:
    _HIVESCANNER_HOME.mkdir(parents=True, exist_ok=True)
    tmp = _SNAPSHOTS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(str(tmp), str(_SNAPSHOTS_FILE))


def load_snapshot(name: str) -> dict:
    """Load named snapshot. Returns {} if missing."""
    return _load_all().get(name, {})


def save_snapshot(name: str, snapshot: dict) -> None:
    """Save named snapshot. Merges with existing snapshots on disk."""
    all_snapshots = _load_all()
    all_snapshots[name] = snapshot
    _save_all(all_snapshots)


class LinearScanner:
    name = "linear"

    def __init__(self):
        self._snapshot = load_snapshot("linear_issues")
        self._bootstrapped = bool(self._snapshot)

    def configure(self) -> dict:
        return {
            "enabled": False,
            "api_key_env": "LINEAR_API_KEY",
            "team_id": "",
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _graphql(self, query: str, variables: dict, api_key: str) -> dict | None:
        data = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            "https://api.linear.app/graphql",
            data=data,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[linear] API error: {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        api_key = os.environ.get(config.get("api_key_env", "LINEAR_API_KEY"), "")
        if not api_key:
            return [], watermark

        team_id = config.get("team_id", "")

        if team_id:
            query = """
            query($teamId: String!, $since: DateTime!) {
              issues(
                filter: { teamId: $teamId, updatedAt: { gte: $since } }
                first: 20
                orderBy: updatedAt
              ) {
                nodes {
                  id identifier title state { name } priority
                  assignee { name email }
                  updatedAt url
                }
              }
            }
            """
            variables = {"teamId": team_id, "since": watermark}
        else:
            query = """
            query($since: DateTime!) {
              issues(
                filter: { updatedAt: { gte: $since } }
                first: 20
                orderBy: updatedAt
              ) {
                nodes {
                  id identifier title state { name } priority
                  assignee { name email }
                  updatedAt url
                }
              }
            }
            """
            variables = {"since": watermark}

        result = self._graphql(query, variables, api_key)
        if not result:
            return [], watermark

        nodes = result.get("data", {}).get("issues", {}).get("nodes", [])
        pollen = []
        is_bootstrap = not self._bootstrapped

        for node in nodes:
            issue_id = node.get("identifier", node.get("id", ""))
            state = node.get("state", {}).get("name", "")
            priority = node.get("priority", 0)
            snapshot_val = f"{state}:{priority}"

            prev = self._snapshot.get(issue_id)
            self._snapshot[issue_id] = snapshot_val

            if is_bootstrap:
                continue
            if prev == snapshot_val:
                continue

            assignee = node.get("assignee", {}) or {}
            pollen.append({
                "id": f"linear-{issue_id}",
                "source": "linear",
                "type": "issue_assigned" if not prev else "issue_updated",
                "title": f"{issue_id}: {node.get('title', '')}"[:100],
                "preview": f"[{state}] {node.get('title', '')}"[:200],
                "discovered_at": self._utc_now_z(),
                "author": assignee.get("email", ""),
                "author_name": assignee.get("name", ""),
                "group": "Issues",
                "url": node.get("url", ""),
                "metadata": {
                    "identifier": issue_id,
                    "state": state,
                    "priority": priority,
                    "prev_state": prev,
                },
            })

        self._bootstrapped = True
        save_snapshot("linear_issues", self._snapshot)
        return pollen, self._utc_now_z()


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = LinearScanner()
    if data["command"] == "poll":
        poll_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": poll_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
