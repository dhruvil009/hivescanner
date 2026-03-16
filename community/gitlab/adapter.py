"""GitLab scanner — monitors GitLab merge requests, CI pipelines, and mentions."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional


class GitLabScanner:
    name = "gitlab"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "GITLAB_TOKEN",
            "gitlab_url": "https://gitlab.com",
            "username": "",
            "max_items": 20,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api(self, path: str, token: str, gitlab_url: str) -> Optional[object]:
        """Call GitLab REST API v4 with PRIVATE-TOKEN header."""
        url = f"{gitlab_url}/api/v4{path}"
        req = urllib.request.Request(
            url,
            headers={
                "PRIVATE-TOKEN": token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[gitlab] API error ({path}): {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        token = os.environ.get(config.get("token_env", "GITLAB_TOKEN"), "")
        if not token:
            return [], watermark

        gitlab_url = config.get("gitlab_url", "https://gitlab.com").rstrip("/")
        username = config.get("username", "")
        max_items = config.get("max_items", 20)

        pollen = []
        had_errors = False

        # --- Merge request reviews ---
        if username:
            mr_path = (
                f"/merge_requests?state=opened"
                f"&reviewer_username={username}"
                f"&updated_after={watermark}"
                f"&per_page={max_items}"
            )
            mrs = self._api(mr_path, token, gitlab_url)
            if mrs is None:
                had_errors = True
            else:
                for mr in mrs:
                    iid = mr.get("iid", "")
                    author = mr.get("author", {}) or {}
                    pollen.append({
                        "id": f"gitlab-mr-{iid}",
                        "source": "gitlab",
                        "type": "gitlab_mr_review",
                        "title": mr.get("title", "")[:100],
                        "preview": f"MR !{iid}: {mr.get('title', '')}"[:200],
                        "discovered_at": self._utc_now_z(),
                        "author": author.get("username", ""),
                        "author_name": author.get("name", ""),
                        "group": "Merge Requests",
                        "url": mr.get("web_url", ""),
                        "metadata": {
                            "iid": iid,
                            "state": mr.get("state", ""),
                            "project_id": mr.get("project_id", ""),
                        },
                    })

        # --- CI pipeline failures ---
        projects = self._api(
            f"/projects?membership=true&with_issues_enabled=false&per_page={max_items}",
            token,
            gitlab_url,
        )
        if projects is None:
            had_errors = True
        else:
            for project in projects:
                proj_id = project.get("id", "")
                pipe_path = (
                    f"/projects/{proj_id}/pipelines"
                    f"?updated_after={watermark}&status=failed&per_page={max_items}"
                )
                pipelines = self._api(pipe_path, token, gitlab_url)
                if pipelines is None:
                    had_errors = True
                    continue
                for pipeline in pipelines:
                    pid = pipeline.get("id", "")
                    pollen.append({
                        "id": f"gitlab-ci-{pid}",
                        "source": "gitlab",
                        "type": "gitlab_ci_failure",
                        "title": f"Pipeline #{pid} failed in {project.get('name', '')}"[:100],
                        "preview": f"Failed pipeline #{pid} on ref {pipeline.get('ref', '')}"[:200],
                        "discovered_at": self._utc_now_z(),
                        "author": "",
                        "author_name": "",
                        "group": "CI Pipelines",
                        "url": pipeline.get("web_url", ""),
                        "metadata": {
                            "pipeline_id": pid,
                            "project_id": proj_id,
                            "project_name": project.get("name", ""),
                            "ref": pipeline.get("ref", ""),
                            "status": pipeline.get("status", ""),
                        },
                    })

        # --- Todos / mentions ---
        todos = self._api("/todos?state=pending", token, gitlab_url)
        if todos is None:
            had_errors = True
        else:
            for todo in todos:
                tid = todo.get("id", "")
                author = todo.get("author", {}) or {}
                target = todo.get("target", {}) or {}
                pollen.append({
                    "id": f"gitlab-todo-{tid}",
                    "source": "gitlab",
                    "type": "gitlab_mention",
                    "title": target.get("title", todo.get("body", ""))[:100],
                    "preview": todo.get("body", "")[:200],
                    "discovered_at": self._utc_now_z(),
                    "author": author.get("username", ""),
                    "author_name": author.get("name", ""),
                    "group": "Mentions",
                    "url": todo.get("target_url", ""),
                    "metadata": {
                        "todo_id": tid,
                        "action_name": todo.get("action_name", ""),
                        "target_type": todo.get("target_type", ""),
                    },
                })

        if had_errors:
            return pollen, watermark
        return pollen, self._utc_now_z()


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = GitLabScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
