"""GitHub scanner — watches PRs, CI, mentions, issues via `gh` CLI."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

# Resolve imports whether run as module or standalone
try:
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists
except ImportError:
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from dep_installer import ensure_tool
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists


def _strict_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_json_loads(value: str) -> object:
    return json.loads(
        value,
        object_pairs_hook=_strict_object,
        parse_constant=_reject_constant,
    )


class GitHubScanner:
    name = "github"
    _POLL_BUDGET_SECONDS = 45
    _CI_STATES = {"ERROR", "EXPECTED", "FAILURE", "PENDING", "SUCCESS"}

    def __init__(self):
        self._cli_available = None
        self._snapshot = load_snapshot("github_notifications")
        self._pr_status_snapshot = load_snapshot("github_pr_statuses")
        self._bootstrapped = snapshot_exists("github_notifications")
        self._ci_bootstrapped = snapshot_exists("github_pr_statuses")
        self._acted_cache = None
        self._token_env = "GITHUB_TOKEN"

    def configure(self) -> dict:
        return {
            "enabled": True,
            "token_env": "GITHUB_TOKEN",
            "username": "",
            "watch_repos": [],
            "watch_reviews": True,
            "watch_ci": True,
            "watch_mentions": True,
            "watch_assignments": True,
            "watch_activity": False,
            "max_items_per_query": 20,
            "max_notification_pages": 10,
            "max_pr_pages": 10,
        }

    def _gh(self, args: list[str], timeout: int = 15) -> str | None:
        """Run gh CLI command, return stdout or None on failure."""
        deadline = getattr(self, "_poll_deadline", None)
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("[github] poll time budget exhausted", file=sys.stderr)
                return None
            timeout = min(timeout, max(0.1, remaining))
        try:
            environment = os.environ.copy()
            token = os.environ.get(self._token_env, "")
            for name in list(environment):
                if name.startswith(("GH_", "GITHUB_")):
                    environment.pop(name, None)
            environment.pop(self._token_env, None)
            environment["GH_HOST"] = "github.com"
            environment["GH_PROMPT_DISABLED"] = "1"
            if token:
                if len(token) > 8192 or any(
                    char.isspace() or ord(char) < 32 or ord(char) == 127
                    for char in token
                ):
                    print("[github] configured token is invalid", file=sys.stderr)
                    return None
                environment["GH_TOKEN"] = token
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                result = subprocess.run(
                    ["gh"] + args,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout,
                    env=environment,
                )
                stdout_file.seek(0)
                raw_stdout = stdout_file.read(5_000_001)
                stderr_file.seek(0)
                raw_stderr = stderr_file.read(201)
            if result.returncode != 0:
                print(
                    f"[github] gh error: {raw_stderr.decode('utf-8', errors='replace')}",
                    file=sys.stderr,
                )
                return None
            if len(raw_stdout) > 5_000_000:
                print("[github] gh output exceeded 5 MB", file=sys.stderr)
                return None
            return raw_stdout.decode("utf-8")
        except (subprocess.TimeoutExpired, OSError, UnicodeError, ValueError) as e:
            print(f"[github] gh failed: {e}", file=sys.stderr)
            return None

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _valid_repo(value: object) -> bool:
        if not isinstance(value, str) or len(value) > 200:
            return False
        parts = value.split("/")
        if len(parts) != 2 or parts[1] in {".", ".."}:
            return False
        owner, repository = parts
        return bool(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner)
            and re.fullmatch(r"[A-Za-z0-9_.-]+", repository)
        )

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 64
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _valid_login(value: object) -> bool:
        return (
            isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", value)
            is not None
        )

    @staticmethod
    def _valid_token_env(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) <= 128
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None
            and value not in {"PATH", "HOME", "SHELL", "PWD", "TMPDIR"}
            and not value.startswith(("LD_", "DYLD_", "PYTHON"))
        )

    @classmethod
    def _valid_notification_component(cls, value: object) -> dict | None:
        if not isinstance(value, dict) or set(value) != {"boundary", "bootstrapped"}:
            return None
        boundary = value.get("boundary")
        bootstrapped = value.get("bootstrapped")
        if (
            not isinstance(boundary, str)
            or (boundary and cls._parse_timestamp(boundary) is None)
            or not isinstance(bootstrapped, bool)
            or (bootstrapped and not boundary)
        ):
            return None
        return {"boundary": boundary, "bootstrapped": bootstrapped}

    @classmethod
    def _valid_legacy_notification_state(cls, value: object) -> bool:
        return (
            isinstance(value, dict)
            and len(value) <= 5_000
            and all(
                isinstance(key, str)
                and 1 <= len(key) <= 128
                and cls._parse_timestamp(timestamp) is not None
                for key, timestamp in value.items()
            )
        )

    @classmethod
    def _valid_ci_component(cls, value: object) -> dict | None:
        if not isinstance(value, dict) or len(value) > 5_002:
            return None
        if set(value) == {"scope", "statuses"}:
            scope = value.get("scope")
            statuses = value.get("statuses")
            if (
                not isinstance(scope, str)
                or re.fullmatch(r"[0-9a-f]{16}", scope) is None
                or not isinstance(statuses, dict)
                or len(statuses) > 5_000
            ):
                return None
            clean_statuses: dict[str, str] = {}
            for key, state in statuses.items():
                if not isinstance(key, str) or "#" not in key:
                    return None
                repo, raw_number = key.rsplit("#", 1)
                if (
                    not cls._valid_repo(repo)
                    or re.fullmatch(r"[1-9]\d{0,9}", raw_number) is None
                    or not isinstance(state, str)
                    or state not in cls._CI_STATES
                ):
                    return None
                clean_statuses[key] = state
            return {"scope": scope, "statuses": clean_statuses}

        # Legacy snapshots used ambiguous repository-number keys. Preserve
        # them only long enough to perform a quiet migration to scoped keys.
        if len(value) > 5_000 or not all(
            isinstance(key, str)
            and 1 <= len(key) <= 300
            and all(ord(char) >= 32 and ord(char) != 127 for char in key)
            and isinstance(state, str)
            and state in cls._CI_STATES
            for key, state in value.items()
        ):
            return None
        return dict(value)

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        if not isinstance(config, dict) or not isinstance(watermark, str):
            return [], watermark
        if watermark and self._parse_timestamp(watermark) is None:
            print("[github] invalid watermark", file=sys.stderr)
            return [], watermark

        token_env = config.get("token_env", "GITHUB_TOKEN")
        if not self._valid_token_env(token_env):
            print("[github] invalid token_env", file=sys.stderr)
            return [], watermark
        self._token_env = token_env

        configured_username = config.get("username", "")
        internal_username = config.get("_username", "")
        if not isinstance(configured_username, str) or not isinstance(
            internal_username, str
        ):
            print("[github] username must be a string", file=sys.stderr)
            return [], watermark
        username = configured_username or internal_username
        if username and not self._valid_login(username):
            print("[github] username must be a GitHub login", file=sys.stderr)
            return [], watermark

        watch_repos = config.get("watch_repos", [])
        if (
            not isinstance(watch_repos, list)
            or len(watch_repos) > 1000
            or not all(self._valid_repo(value) for value in watch_repos)
            or len(set(watch_repos)) != len(watch_repos)
        ):
            print(
                "[github] watch_repos must contain unique owner/repository names",
                file=sys.stderr,
            )
            return [], watermark

        watch_mentions = config.get("watch_mentions", True)
        watch_reviews = config.get("watch_reviews", True)
        watch_assignments = config.get("watch_assignments", True)
        watch_activity = config.get("watch_activity", False)
        watch_ci = config.get("watch_ci", True)
        if any(
            type(value) is not bool
            for value in (
                watch_mentions,
                watch_reviews,
                watch_assignments,
                watch_activity,
                watch_ci,
            )
        ):
            print("[github] watch flags must be booleans", file=sys.stderr)
            return [], watermark

        max_items_per_query = config.get("max_items_per_query", 20)
        max_notification_pages = config.get("max_notification_pages", 10)
        max_pr_pages = config.get("max_pr_pages", 10)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (
                    max_items_per_query,
                    max_notification_pages,
                    max_pr_pages,
                )
            )
            or not 1 <= max_items_per_query <= 100
            or not 1 <= max_notification_pages <= 10
            or not 1 <= max_pr_pages <= 10
        ):
            print("[github] pagination limits are invalid", file=sys.stderr)
            return [], watermark

        prepared_ci = self._prepare_state(
            self._pr_status_snapshot, watermark, self._ci_bootstrapped
        )
        notification_committed = self._prepare_notification_state(
            self._snapshot, watermark, self._bootstrapped
        )
        if prepared_ci is None or notification_committed is None:
            print("[github] invalid persisted snapshot; preserving watermark", file=sys.stderr)
            return [], watermark
        ci_committed, ci_bootstrap = prepared_ci
        next_ci_state = ci_committed

        if self._cli_available is None:
            self._cli_available = ensure_tool("gh")
        if not self._cli_available:
            return [], watermark

        # Reset acted cache each poll cycle.
        self._acted_cache = None
        scan_started_at = self._utc_now_z()
        items: list[dict] = []
        notification_success = False
        ci_success = False

        # --- Notifications (covers mentions, review requests) ---
        if any((
            watch_mentions,
            watch_reviews,
            watch_assignments,
            watch_activity,
        )):
            try:
                notif_items = self._poll_notifications(
                    {**config, "watch_repos": watch_repos},
                    notification_committed["boundary"],
                    not notification_committed["bootstrapped"],
                )
                items.extend(notif_items)
                notification_success = True
            except Exception as e:
                print(f"[github] Notification poll error: {e}", file=sys.stderr)
        else:
            # Disabled notification categories intentionally skip their
            # history, so re-enabling cannot create a surprise flood.
            notification_success = True

        # --- CI status on user's open PRs ---
        if watch_ci:
            try:
                ci_items, next_ci_state = self._poll_ci_status(
                    username, watch_repos, ci_committed, ci_bootstrap, config
                )
                items.extend(ci_items)
                ci_success = True
            except Exception as e:
                print(f"[github] CI poll error: {e}", file=sys.stderr)

        # Each component stages its own cursor. Advancing the scanner-loop
        # watermark after one component fails is therefore safe: the failed
        # component retains its prior cursor while healthy siblings commit.
        self._snapshot = {
            "schema_version": 3,
            "committed": notification_committed,
            "candidate": (
                {"boundary": scan_started_at, "bootstrapped": True}
                if notification_success
                else notification_committed
            ),
            "candidate_watermark": scan_started_at,
        }
        save_snapshot("github_notifications", self._snapshot)
        if ci_success:
            self._pr_status_snapshot = {
                "schema_version": 2,
                "committed": ci_committed,
                "candidate": next_ci_state,
                "candidate_watermark": scan_started_at,
                "bootstrap_pending": ci_bootstrap,
            }
            save_snapshot("github_pr_statuses", self._pr_status_snapshot)
        self._bootstrapped = True
        if ci_success:
            self._ci_bootstrapped = True

        return items, scan_started_at

    @classmethod
    def _prepare_notification_state(
        cls, snapshot: dict, watermark: str, initialized: bool
    ) -> dict | None:
        if snapshot.get("schema_version") == 3:
            if (
                type(snapshot.get("schema_version")) is not int
                or set(snapshot)
                != {
                    "schema_version",
                    "committed",
                    "candidate",
                    "candidate_watermark",
                }
            ):
                return None
            committed = cls._valid_notification_component(snapshot.get("committed"))
            candidate = cls._valid_notification_component(snapshot.get("candidate"))
            candidate_wm = snapshot.get("candidate_watermark")
            if (
                committed is None
                or candidate is None
                or cls._parse_timestamp(candidate_wm) is None
            ):
                return None
            current_time = cls._parse_timestamp(watermark) if watermark else None
            candidate_time = cls._parse_timestamp(candidate_wm)
            if current_time is not None and current_time >= candidate_time:
                committed = candidate
            return committed

        bootstrap = not initialized
        if snapshot.get("schema_version") == 2:
            if (
                type(snapshot.get("schema_version")) is not int
                or not isinstance(snapshot.get("bootstrap_pending"), bool)
                or not isinstance(snapshot.get("candidate_watermark"), str)
                or (
                    snapshot.get("candidate_watermark")
                    and cls._parse_timestamp(snapshot["candidate_watermark"]) is None
                )
            ):
                return None
            bootstrap = snapshot["bootstrap_pending"]
            candidate_wm = snapshot["candidate_watermark"]
            current_time = cls._parse_timestamp(watermark) if watermark else None
            candidate_time = cls._parse_timestamp(candidate_wm) if candidate_wm else None
            if (
                bootstrap
                and current_time is not None
                and candidate_time is not None
                and current_time >= candidate_time
            ):
                bootstrap = False
        elif "schema_version" in snapshot:
            return None
        elif (
            not cls._valid_legacy_notification_state(snapshot)
            or (not initialized and snapshot)
        ):
            return None
        return {
            "boundary": watermark,
            "bootstrapped": not bootstrap,
        }

    @classmethod
    def _prepare_state(
        cls, snapshot: dict, watermark: str, initialized: bool
    ) -> tuple[dict, bool] | None:
        if snapshot.get("schema_version") == 2:
            if (
                type(snapshot.get("schema_version")) is not int
                or set(snapshot)
                != {
                    "schema_version",
                    "committed",
                    "candidate",
                    "candidate_watermark",
                    "bootstrap_pending",
                }
                or not isinstance(snapshot.get("bootstrap_pending"), bool)
            ):
                return None
            committed = cls._valid_ci_component(snapshot.get("committed"))
            candidate = cls._valid_ci_component(snapshot.get("candidate"))
            candidate_wm = snapshot.get("candidate_watermark")
            if (
                committed is None
                or candidate is None
                or cls._parse_timestamp(candidate_wm) is None
            ):
                return None
            current_time = cls._parse_timestamp(watermark) if watermark else None
            candidate_time = cls._parse_timestamp(candidate_wm)
            if current_time is not None and current_time >= candidate_time:
                committed = candidate
            bootstrap = snapshot["bootstrap_pending"]
            if bootstrap and current_time is not None and current_time >= candidate_time:
                bootstrap = False
            return committed, bootstrap
        if "schema_version" in snapshot:
            return None
        legacy = cls._valid_ci_component(snapshot)
        if legacy is None or (not initialized and snapshot):
            return None
        return legacy, not initialized

    def _poll_notifications(
        self, config: dict, watermark: str, is_bootstrap: bool | None = None
    ) -> list[dict]:
        """Fetch notifications since watermark."""
        page_size = config.get("max_items_per_query", 20)
        max_pages = config.get("max_notification_pages", 10)
        request_since = watermark
        if watermark and not watermark.startswith("1970-"):
            parsed_watermark = self._parse_timestamp(watermark)
            if parsed_watermark is None:
                raise RuntimeError("GitHub notification watermark is invalid")
            request_since = (parsed_watermark - timedelta(minutes=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        notifications: list[dict] = []
        for page_number in range(1, max_pages + 2):
            args = [
                "api",
                "--method",
                "GET",
                "/notifications",
                "-f",
                f"per_page={page_size}",
                "-f",
                f"page={page_number}",
                "-f",
                "all=true",
            ]
            if request_since and not request_since.startswith("1970-"):
                args.extend(["-f", f"since={request_since}"])
            raw = self._gh(args, timeout=30)
            if raw is None:
                raise RuntimeError("GitHub notifications request failed")
            try:
                page = _strict_json_loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError("GitHub notifications returned invalid JSON") from exc
            if not isinstance(page, list) or not all(
                isinstance(value, dict) for value in page
            ) or len(page) > page_size:
                raise RuntimeError("GitHub notifications returned an invalid response shape")
            if page_number > max_pages:
                if page:
                    raise RuntimeError(
                        "GitHub notifications exceeded max_notification_pages"
                    )
                break
            notifications.extend(page)
            if len(page) < page_size:
                break

        items = []
        if is_bootstrap is None:
            is_bootstrap = not self._bootstrapped

        seen_notifications: dict[str, str] = {}
        for notif in notifications:
            notif_id = notif.get("id")
            updated = notif.get("updated_at")
            subject = notif.get("subject")
            repository = notif.get("repository")
            reason = notif.get("reason")
            if (
                not isinstance(notif_id, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", notif_id) is None
                or self._parse_timestamp(updated) is None
                or not isinstance(subject, dict)
                or not isinstance(repository, dict)
                or not isinstance(reason, str)
                or re.fullmatch(r"[a-z_]{1,128}", reason) is None
            ):
                raise RuntimeError("GitHub notification omitted required fields")
            if notif_id in seen_notifications:
                if seen_notifications[notif_id] != updated:
                    raise RuntimeError("GitHub notification changed during pagination")
                continue
            seen_notifications[notif_id] = updated
            repo_name = repository.get("full_name")
            if not self._valid_repo(repo_name):
                raise RuntimeError("GitHub notification contained an invalid repository")
            if is_bootstrap:
                continue

            raw_title = subject.get("title")
            subject_type = subject.get("type")
            raw_url = subject.get("url")
            if (
                not isinstance(raw_title, str)
                or len(raw_title) > 100_000
                or not isinstance(subject_type, str)
                or not 1 <= len(subject_type) <= 128
                or (raw_url is not None and not isinstance(raw_url, str))
                or (isinstance(raw_url, str) and len(raw_url) > 2_000)
            ):
                raise RuntimeError("GitHub notification subject was malformed")
            title = raw_title[:100]
            url_path = raw_url or ""

            # Determine pollen type
            if reason == "review_requested":
                pollen_type = "review_needed"
                group = "Reviews"
                preview = f"Review requested on: {title}"
            elif reason in {"mention", "team_mention"}:
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
            web_url = self._api_url_to_web(url_path, repo_name)

            watch_repos = config["watch_repos"]
            if watch_repos and repo_name not in watch_repos:
                continue

            if reason == "review_requested" and not config.get("watch_reviews", True):
                continue
            if reason in {"mention", "team_mention"} and not config.get("watch_mentions", True):
                continue
            if reason == "assign" and not config.get("watch_assignments", True):
                continue
            if reason not in {"review_requested", "mention", "team_mention", "assign"} and not config.get("watch_activity", False):
                continue

            metadata = {
                "notification_id": notif_id,
                "reason": reason,
                "subject_type": subject_type,
                "repo": repo_name,
                "notification_updated_at": updated,
            }
            if subject_type == "PullRequest":
                pr_number = self._pr_number_from_api_url(url_path)
                if pr_number is not None:
                    metadata["pr_number"] = pr_number

            event_fingerprint = f"{reason}:{updated}"
            update_hash = hashlib.sha256(event_fingerprint.encode()).hexdigest()[:8]
            item_id = f"github-{pollen_type}-{notif_id}-{update_hash}"

            items.append({
                "id": item_id,
                "source": "github",
                "type": pollen_type,
                "title": title,
                "preview": preview[:200],
                "discovered_at": self._utc_now_z(),
                "author": "",
                "author_name": "",
                "group": group,
                "url": web_url,
                "metadata": metadata,
            })

        return items

    @staticmethod
    def _pr_number_from_api_url(api_url: str) -> int | None:
        """Extract PR number from API URL like https://api.github.com/repos/{owner}/{repo}/pulls/{number}."""
        if not isinstance(api_url, str):
            return None
        match = re.fullmatch(
            r"https://api\.github\.com/repos/([^/?#]+)/([^/?#]+)/pulls/([1-9]\d{0,9})",
            api_url,
        )
        if match is None or not GitHubScanner._valid_repo(
            f"{match.group(1)}/{match.group(2)}"
        ):
            return None
        return int(match.group(3))

    def _poll_ci_status(
        self,
        username: str,
        watch_repos: list[str],
        committed_state: dict,
        is_bootstrap: bool,
        config: dict,
    ) -> tuple[list[dict], dict]:
        """Check CI status on user's open PRs."""
        query = """
        query($cursor: String) {
          viewer {
            login
            pullRequests(states: OPEN, first: 100, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
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
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        max_pages = config.get("max_pr_pages", 10)
        prs = []
        cursor = ""
        viewer_login = username
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            args = ["api", "graphql", "-f", f"query={query}"]
            if cursor:
                args.extend(["-f", f"cursor={cursor}"])
            raw = self._gh(args, timeout=20)
            if raw is None:
                raise RuntimeError("GitHub GraphQL request failed")
            try:
                data = _strict_json_loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError("GitHub GraphQL returned invalid JSON") from exc
            if not isinstance(data, dict) or data.get("errors"):
                raise RuntimeError("GitHub GraphQL returned errors")
            data_obj = data.get("data")
            viewer = data_obj.get("viewer") if isinstance(data_obj, dict) else None
            connection = viewer.get("pullRequests") if isinstance(viewer, dict) else None
            nodes = connection.get("nodes") if isinstance(connection, dict) else None
            raw_viewer_login = viewer.get("login") if isinstance(viewer, dict) else None
            if (
                not self._valid_login(raw_viewer_login)
                or not isinstance(nodes, list)
                or len(nodes) > 100
                or not all(isinstance(node, dict) for node in nodes)
            ):
                raise RuntimeError("GitHub GraphQL returned invalid pull request data")
            viewer_login = raw_viewer_login
            prs.extend(nodes)
            page_info = connection.get("pageInfo")
            if not isinstance(page_info, dict) or not isinstance(
                page_info.get("hasNextPage"), bool
            ):
                raise RuntimeError("GitHub GraphQL returned invalid pageInfo")
            if not page_info["hasNextPage"]:
                break
            raw_cursor = page_info.get("endCursor")
            if (
                not isinstance(raw_cursor, str)
                or not raw_cursor
                or len(raw_cursor) > 2000
                or any(ord(char) < 32 or ord(char) == 127 for char in raw_cursor)
                or raw_cursor in seen_cursors
            ):
                raise RuntimeError("GitHub GraphQL pagination omitted endCursor")
            seen_cursors.add(raw_cursor)
            cursor = raw_cursor
        else:
            raise RuntimeError("GitHub open PR list exceeded max_pr_pages")

        scope = hashlib.sha256(
            json.dumps(
                {"viewer": viewer_login, "watch_repos": sorted(watch_repos)},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]
        if isinstance(committed_state.get("statuses"), dict):
            previous_statuses = (
                committed_state["statuses"]
                if committed_state.get("scope") == scope
                else {}
            )
            is_bootstrap = is_bootstrap or committed_state.get("scope") != scope
        else:
            previous_statuses = committed_state
            if committed_state:
                # The legacy key format was ambiguous; bootstrap once into
                # source-qualified PR keys instead of emitting false changes.
                is_bootstrap = True
        items = []
        next_statuses = {}
        seen_pr_keys: set[str] = set()

        for pr in prs:
            repository = pr.get("repository")
            repo = (
                repository.get("nameWithOwner")
                if isinstance(repository, dict)
                else None
            )
            if not self._valid_repo(repo):
                raise RuntimeError("GitHub GraphQL returned an invalid repository")
            if watch_repos and repo not in watch_repos:
                continue
            raw_number = pr.get("number")
            if (
                isinstance(raw_number, bool)
                or not isinstance(raw_number, int)
                or not 1 <= raw_number <= 9_999_999_999
            ):
                raise RuntimeError("GitHub GraphQL returned an invalid PR number")
            number = raw_number
            pr_key = f"{repo}#{number}"
            if pr_key in seen_pr_keys:
                raise RuntimeError("GitHub GraphQL returned a duplicate PR")
            seen_pr_keys.add(pr_key)
            raw_sha = pr.get("headRefOid")
            if not isinstance(raw_sha, str) or not re.fullmatch(
                r"[0-9a-fA-F]{40,64}", raw_sha
            ):
                raise RuntimeError("GitHub GraphQL returned an invalid head OID")
            sha = raw_sha[:12]
            raw_title = pr.get("title")
            commits_obj = pr.get("commits")
            commits = commits_obj.get("nodes") if isinstance(commits_obj, dict) else None
            if (
                not isinstance(raw_title, str)
                or len(raw_title) > 100_000
                or not isinstance(commits, list)
                or len(commits) > 1
                or not all(isinstance(value, dict) for value in commits)
            ):
                raise RuntimeError("GitHub GraphQL returned malformed PR details")
            title = raw_title[:100]
            if not commits:
                if pr_key in previous_statuses:
                    next_statuses[pr_key] = previous_statuses[pr_key]
                continue
            commit = commits[0].get("commit")
            if not isinstance(commit, dict):
                raise RuntimeError("GitHub GraphQL returned malformed commit data")
            rollup = commit.get("statusCheckRollup")
            if rollup is not None and not isinstance(rollup, dict):
                raise RuntimeError("GitHub GraphQL returned malformed status rollup")
            state = rollup.get("state") if rollup else ""
            if not state:
                if pr_key in previous_statuses:
                    next_statuses[pr_key] = previous_statuses[pr_key]
                continue
            if not isinstance(state, str) or state not in self._CI_STATES:
                raise RuntimeError("GitHub GraphQL returned an unknown CI state")

            prev_state = previous_statuses.get(pr_key)
            next_statuses[pr_key] = state

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

            transition = f"{prev_state or 'none'}-{state}-{sha}"
            transition_hash = hashlib.sha256(transition.encode()).hexdigest()[:10]
            items.append({
                "id": (
                    f"github-ci-{hashlib.sha256(repo.encode()).hexdigest()[:12]}-"
                    f"{number}-{sha}-{transition_hash}"
                ),
                "source": "github",
                "type": pollen_type,
                "title": f"PR #{number}: {title}",
                "preview": preview[:200],
                "discovered_at": self._utc_now_z(),
                "author": viewer_login,
                "author_name": viewer_login,
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

        return items, {"scope": scope, "statuses": next_statuses}

    def _api_url_to_web(self, api_url: str, expected_repo: str = "") -> str:
        """Convert GitHub API URL to web URL."""
        if not isinstance(api_url, str) or not api_url:
            return ""
        match = re.fullmatch(
            r"https://api\.github\.com/repos/([^/?#]+)/([^/?#]+)/"
            r"([A-Za-z0-9._~!$&'()*+,;=:@/-]+)",
            api_url,
        )
        if match is None:
            return ""
        repo = f"{match.group(1)}/{match.group(2)}"
        remainder = match.group(3)
        if (
            not self._valid_repo(repo)
            or (expected_repo and repo != expected_repo)
            or any(segment in {"", ".", ".."} for segment in remainder.split("/"))
        ):
            return ""
        if remainder.startswith("pulls/"):
            remainder = f"pull/{remainder.removeprefix('pulls/')}"
        return f"https://github.com/{repo}/{remainder}"

    def check_acted(self, pollen: dict, config: dict) -> bool:
        """Check if user has reviewed a PR they were requested to review."""
        if (
            not isinstance(pollen, dict)
            or not isinstance(config, dict)
            or pollen.get("type") != "review_needed"
        ):
            return False

        username = config.get("username", "") or config.get("_username", "")
        if not self._valid_login(username):
            return False
        token_env = config.get("token_env", "GITHUB_TOKEN")
        if not self._valid_token_env(token_env):
            return False
        self._token_env = token_env

        meta = pollen.get("metadata", {})
        if not isinstance(meta, dict):
            return False
        repo = meta.get("repo", "")
        pr_number = meta.get("pr_number")

        if (
            not self._valid_repo(repo)
            or isinstance(pr_number, bool)
            or not isinstance(pr_number, int)
            or not 1 <= pr_number <= 9_999_999_999
        ):
            return False

        # Cache lookup — avoid re-querying same PR within a cycle
        if self._acted_cache is None:
            self._acted_cache = {}

        cache_key = f"{repo}#{pr_number}"
        if cache_key in self._acted_cache:
            return self._acted_cache[cache_key]

        raw = self._gh([
            "api",
            "--method",
            "GET",
            "--paginate",
            "--slurp",
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            "-f",
            "per_page=100",
        ])
        acted = False
        if raw:
            try:
                reviews = _strict_json_loads(raw)
            except (json.JSONDecodeError, ValueError):
                return False
            if not isinstance(reviews, list):
                return False
            if reviews and all(isinstance(page, list) for page in reviews):
                reviews = [review for page in reviews for review in page]
            if len(reviews) > 10_000 or not all(
                isinstance(review, dict) for review in reviews
            ):
                return False
            requested_at = meta.get("notification_updated_at", "")
            if requested_at and self._parse_timestamp(requested_at) is None:
                return False
            requested_dt = self._parse_timestamp(requested_at) if requested_at else None
            for review in reviews:
                review_user = review.get("user")
                if review_user is None:
                    continue
                if not isinstance(review_user, dict):
                    return False
                review_login = review_user.get("login")
                if not self._valid_login(review_login):
                    return False
                if review_login.casefold() != username.casefold():
                    continue
                submitted_at = review.get("submitted_at")
                if submitted_at is None:
                    continue
                submitted_dt = self._parse_timestamp(submitted_at)
                if submitted_dt is None:
                    return False
                submitted_after_request = (
                    requested_dt is None or submitted_dt >= requested_dt
                )
                if submitted_after_request:
                    acted = True
                    break
        self._acted_cache[cache_key] = acted
        return acted
