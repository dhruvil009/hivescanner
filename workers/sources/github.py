"""GitHub scanner — watches PRs, CI, mentions, issues via `gh` CLI."""

import json
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

# Resolve imports whether run as module or standalone
try:
    from snapshot_store import load_snapshot, save_snapshot
except ImportError:
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from snapshot_store import load_snapshot, save_snapshot


class GitHubScanner:
    name = "github"

    def __init__(self):
        self._cli_available = None
        self._snapshot = load_snapshot("github_notifications")
        self._pr_status_snapshot = load_snapshot("github_pr_statuses")
        self._bootstrapped = bool(self._snapshot)
        self._acted_cache = None

    def configure(self) -> dict:
        return {
            "enabled": True,
            "token_env": "GITHUB_TOKEN",
            "username": "",
            "watch_repos": [],
            "watch_reviews": True,
            "watch_ci": True,
            "watch_mentions": True,
            "max_items_per_query": 20,
        }

    def _gh(self, args: list[str], timeout: int = 15) -> str | None:
        """Run gh CLI command, return stdout or None on failure."""
        try:
            result = subprocess.run(
                ["gh"] + args,
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                print(f"[github] gh error: {result.stderr[:200]}", file=sys.stderr)
                return None
            return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"[github] gh failed: {e}", file=sys.stderr)
            return None

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        if self._cli_available is None:
            self._cli_available = shutil.which("gh") is not None
        if not self._cli_available:
            return [], watermark

        # Reset acted cache each poll cycle
        self._acted_cache = None

        items = []
        had_errors = False
        username = config.get("username", "")
        watch_repos = config.get("watch_repos", [])

        # --- Notifications (covers mentions, review requests) ---
        if config.get("watch_mentions", True) or config.get("watch_reviews", True):
            try:
                notif_items = self._poll_notifications(config, watermark)
                items.extend(notif_items)
            except Exception as e:
                print(f"[github] Notification poll error: {e}", file=sys.stderr)
                had_errors = True

        # --- CI status on user's open PRs ---
        if config.get("watch_ci", True) and username:
            try:
                ci_items = self._poll_ci_status(username, watch_repos)
                items.extend(ci_items)
            except Exception as e:
                print(f"[github] CI poll error: {e}", file=sys.stderr)
                had_errors = True

        # Save snapshots
        save_snapshot("github_notifications", self._snapshot)
        save_snapshot("github_pr_statuses", self._pr_status_snapshot)
        self._bootstrapped = True

        # Batch grouping
        items = self._batch_by_author(items)

        if had_errors:
            return items, watermark
        return items, self._utc_now_z()

    def _poll_notifications(self, config: dict, watermark: str) -> list[dict]:
        """Fetch notifications since watermark."""
        raw = self._gh(["api", "/notifications", "--jq", "."])
        if raw is None:
            return []
        try:
            notifications = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(notifications, list):
            return []

        items = []
        is_bootstrap = not self._bootstrapped

        for notif in notifications:
            notif_id = notif.get("id", "")
            updated = notif.get("updated_at", "")
            if updated and updated <= watermark:
                continue

            # Dedup via snapshot
            prev = self._snapshot.get(notif_id)
            self._snapshot[notif_id] = updated
            if is_bootstrap:
                continue
            if prev == updated:
                continue

            subject = notif.get("subject", {})
            reason = notif.get("reason", "")
            title = subject.get("title", "")[:100]
            subject_type = subject.get("type", "")
            url_path = subject.get("url", "")

            # Determine pollen type
            if reason == "review_requested":
                pollen_type = "review_needed"
                group = "Reviews"
                preview = f"Review requested on: {title}"
            elif reason == "mention":
                pollen_type = "mention"
                group = "Mentions"
                preview = f"You were mentioned: {title}"
            elif reason == "assign":
                pollen_type = "issue_assigned"
                group = "Issues"
                preview = f"Assigned to you: {title}"
            else:
                pollen_type = "notification"
                group = "Activity"
                preview = f"{reason}: {title}"

            # Build web URL from API URL
            web_url = self._api_url_to_web(url_path)

            repo_name = notif.get("repository", {}).get("full_name", "")

            items.append({
                "id": f"github-{pollen_type}-{notif_id}",
                "source": "github",
                "type": pollen_type,
                "title": title,
                "preview": preview[:200],
                "discovered_at": self._utc_now_z(),
                "author": "",
                "author_name": "",
                "group": group,
                "url": web_url,
                "metadata": {
                    "notification_id": notif_id,
                    "reason": reason,
                    "subject_type": subject_type,
                    "repo": repo_name,
                },
            })

        return items

    def _poll_ci_status(self, username: str, watch_repos: list[str]) -> list[dict]:
        """Check CI status on user's open PRs."""
        query = """
        query {
          viewer {
            pullRequests(states: OPEN, first: 20, orderBy: {field: UPDATED_AT, direction: DESC}) {
              nodes {
                number
                title
                headRefOid
                repository { nameWithOwner }
                commits(last: 1) {
                  nodes {
                    commit {
                      statusCheckRollup {
                        state
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        raw = self._gh(["api", "graphql", "-f", f"query={query}"], timeout=20)
        if raw is None:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        prs = (data.get("data", {}).get("viewer", {})
               .get("pullRequests", {}).get("nodes", []))
        if not prs:
            return []

        items = []
        is_bootstrap = not bool(self._pr_status_snapshot)

        for pr in prs:
            repo = pr.get("repository", {}).get("nameWithOwner", "")
            if watch_repos and repo not in watch_repos:
                continue
            number = pr.get("number", 0)
            sha = pr.get("headRefOid", "")[:8]
            title = pr.get("title", "")[:100]
            commits = pr.get("commits", {}).get("nodes", [])
            if not commits:
                continue
            rollup = (commits[0].get("commit", {})
                      .get("statusCheckRollup", {}))
            state = rollup.get("state", "") if rollup else ""
            if not state:
                continue

            pr_key = f"{repo}-{number}"
            prev_state = self._pr_status_snapshot.get(pr_key)
            self._pr_status_snapshot[pr_key] = state

            if is_bootstrap:
                continue
            if prev_state == state:
                continue

            if state == "FAILURE" or state == "ERROR":
                pollen_type = "ci_failure"
                preview = f"CI failed on PR #{number}: {title}"
            elif state == "SUCCESS" and prev_state in ("FAILURE", "ERROR", "PENDING"):
                pollen_type = "ci_passed"
                preview = f"CI passed on PR #{number}: {title}"
            else:
                continue

            items.append({
                "id": f"github-ci-{repo}-{sha}",
                "source": "github",
                "type": pollen_type,
                "title": f"PR #{number}: {title}",
                "preview": preview[:200],
                "discovered_at": self._utc_now_z(),
                "author": username,
                "author_name": username,
                "group": "CI",
                "url": f"https://github.com/{repo}/pull/{number}",
                "metadata": {
                    "pr_number": number,
                    "repo": repo,
                    "sha": sha,
                    "state": state,
                    "prev_state": prev_state,
                },
            })

        return items

    def _api_url_to_web(self, api_url: str) -> str:
        """Convert GitHub API URL to web URL."""
        if not api_url:
            return ""
        url = api_url.replace("https://api.github.com/repos/", "https://github.com/")
        url = url.replace("/pulls/", "/pull/")
        return url

    def check_acted(self, pollen: dict, config: dict) -> bool:
        """Check if user has reviewed a PR they were requested to review."""
        if pollen.get("type") != "review_needed":
            return False

        username = config.get("_username", "")
        if not username:
            return False

        meta = pollen.get("metadata", {})
        repo = meta.get("repo", "")
        pr_number = meta.get("pr_number")

        if not repo or not pr_number:
            return False

        # Cache lookup — avoid re-querying same PR within a cycle
        if self._acted_cache is None:
            self._acted_cache = {}

        cache_key = f"{repo}#{pr_number}"
        if cache_key in self._acted_cache:
            return self._acted_cache[cache_key]

        # Query reviews for this PR, filtering by username via jq
        # Username is sanitized to prevent jq injection
        safe_username = "".join(c for c in username if c.isalnum() or c in "-_")
        raw = self._gh(["api", f"/repos/{repo}/pulls/{pr_number}/reviews",
                       "--jq", f'[.[] | select(.user.login == "{safe_username}")] | length'])
        acted = bool(raw and raw.strip() != "0")
        self._acted_cache[cache_key] = acted
        return acted

    @staticmethod
    def _batch_by_author(items: list[dict], threshold: int = 5) -> list[dict]:
        """Collapse N+ items from same author into a summary."""
        by_author = defaultdict(list)
        for item in items:
            by_author[item.get("author", "")].append(item)
        result = []
        for author, author_items in by_author.items():
            if len(author_items) >= threshold:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                result.append({
                    "id": f"github-batch-{author}-{now}",
                    "source": "github",
                    "type": "review_needed_batch",
                    "title": f"{len(author_items)} items from {author or 'unknown'}",
                    "preview": f"Batch: {len(author_items)} items from {author or 'unknown'} need attention",
                    "discovered_at": now,
                    "author": author,
                    "author_name": author,
                    "group": "Reviews",
                    "url": "",
                    "metadata": {
                        "item_ids": [i["id"] for i in author_items],
                        "count": len(author_items),
                    },
                })
            else:
                result.extend(author_items)
        return result
