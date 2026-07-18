"""GitLab scanner for review requests, failed pipelines, and pending todos."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        try:
            source_port = source.port or (443 if source.scheme == "https" else 80)
            target_port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            target_port = -1
            source_port = -2
        if (
            source.scheme.casefold() != target.scheme.casefold()
            or (source.hostname or "").casefold() != (target.hostname or "").casefold()
            or source_port != target_port
        ):
            raise urllib.error.HTTPError(
                newurl, code, "cross-origin redirect refused", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_SameOriginRedirectHandler()).open(
        req, timeout=timeout
    )


def _reject_duplicate_key(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _strict_json(raw: bytes | str) -> object:
    return json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_key,
        parse_constant=_reject_constant,
    )


def _bounded_text(value: object, maximum: int, *, allow_empty: bool = True) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= maximum
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _positive_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= 10**18 else None
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and 1 <= len(value) <= 19
    ):
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _valid_person(value: object) -> bool:
    return (
        isinstance(value, dict)
        and _bounded_text(value.get("username", ""), 255)
        and _bounded_text(value.get("name", ""), 10_000)
    )


class GitLabScanner:
    name = "gitlab"
    _POLL_BUDGET_SECONDS = 45

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "GITLAB_TOKEN",
            "gitlab_url": "https://gitlab.com",
            "username": "",
            "watch_projects": [],
            "watch_reviews": True,
            "watch_pipelines": True,
            "watch_todos": True,
            "max_items": 100,
            "max_pages": 3,
            "projects_per_poll": 3,
            "overlap_minutes": 10,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _valid_base_url(value: object) -> str | None:
        if not isinstance(value, str) or value != value.strip():
            return None
        raw = value.rstrip("/")
        parsed = urllib.parse.urlsplit(raw)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if port is not None and not 1 <= port <= 65535:
            return None
        # Protect credentials from accidental plaintext transport, while still
        # allowing a local development GitLab instance.
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return None
        return raw

    def _api(self, path: str, token: str, gitlab_url: str) -> Optional[object]:
        url = f"{gitlab_url}/api/v4{path}"
        req = urllib.request.Request(
            url,
            headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
        )
        deadline = getattr(self, "_poll_deadline", None)
        timeout = 15.0
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            timeout = min(timeout, max(0.1, remaining))
        try:
            with _urlopen(req, timeout=timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("response exceeded 2 MB")
                return _strict_json(raw)
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "")
            suffix = f"; retry after {retry_after}s" if retry_after else ""
            print(f"[gitlab] HTTP {exc.code}{suffix}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[gitlab] API error: {exc}", file=sys.stderr)
        return None

    def _paginate(
        self,
        base_path: str,
        params: dict,
        token: str,
        gitlab_url: str,
        per_page: int,
        max_pages: int,
    ) -> list[dict] | None:
        result: list[dict] = []
        for page in range(1, max_pages + 2):
            query = urllib.parse.urlencode({**params, "per_page": per_page, "page": page})
            data = self._api(f"{base_path}?{query}", token, gitlab_url)
            if not isinstance(data, list):
                return None
            if not all(isinstance(value, dict) for value in data):
                return None
            if page > max_pages:
                if data:
                    print(f"[gitlab] pagination exceeded {max_pages} pages for {base_path}", file=sys.stderr)
                    return None
                return result
            result.extend(data)
            if len(data) < per_page:
                return result
        return result

    @staticmethod
    def _load_state(watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") in {2, 3, 4, 5}:
            initialized = state.get("initialized")
            legacy_project_scheduler = "project_next_index" not in state
            state.setdefault("merge_requests", {})
            state.setdefault("todo_max_id", 0)
            state.setdefault("project_updated_at", {})
            state.setdefault("project_next_index", 0)
            state.setdefault("reviews_initialized", bool(initialized))
            state.setdefault("todos_initialized", bool(initialized))
            if state.get("version") == 5:
                state.setdefault("todo_ids", [])
                state.setdefault("todo_set_initialized", False)
            merge_requests = state["merge_requests"]
            project_times = state["project_updated_at"]
            todo_max_id = state["todo_max_id"]
            project_index = state["project_next_index"]
            todo_ids = state.get("todo_ids", [])
            if (
                not isinstance(initialized, bool)
                or not isinstance(merge_requests, dict)
                or len(merge_requests) > 500
                or not all(
                    _bounded_text(key, 260, allow_empty=False)
                    and _bounded_text(value, 128, allow_empty=False)
                    for key, value in merge_requests.items()
                )
                or not isinstance(project_times, dict)
                or len(project_times) > 100
                or not all(
                    _bounded_text(key, 255, allow_empty=False)
                    and _bounded_text(value, 128, allow_empty=False)
                    for key, value in project_times.items()
                )
                or isinstance(todo_max_id, bool)
                or not isinstance(todo_max_id, int)
                or not 0 <= todo_max_id <= 10**18
                or isinstance(project_index, bool)
                or not isinstance(project_index, int)
                or not 0 <= project_index <= 1_000_000
                or not isinstance(state["reviews_initialized"], bool)
                or not isinstance(state["todos_initialized"], bool)
                or (
                    state.get("version") == 5
                    and not isinstance(state.get("todo_set_initialized"), bool)
                )
                or not isinstance(todo_ids, list)
                or len(todo_ids) > 500
                or not all(_positive_id(value) is not None for value in todo_ids)
                or len({_positive_id(value) for value in todo_ids}) != len(todo_ids)
                or (
                    "scope" in state
                    and not (
                        isinstance(state["scope"], str)
                        and re.fullmatch(r"[0-9a-f]{16}", state["scope"])
                    )
                )
            ):
                return None
            state["_legacy_project_scheduler"] = legacy_project_scheduler
            return state
        if isinstance(watermark, str) and watermark.lstrip().startswith(("{", "[")):
            return None
        legacy = watermark if isinstance(watermark, str) and watermark else "1970-01-01T00:00:00Z"
        return {
            "version": 4,
            "updated_at": legacy,
            "initialized": not legacy.startswith("1970-"),
            "merge_requests": {},
            "todo_max_id": 0,
            "project_updated_at": {},
            "project_next_index": 0,
            "_legacy_project_scheduler": True,
            "reviews_initialized": False,
            "todos_initialized": False,
        }

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        token_env = config.get("token_env", "GITLAB_TOKEN")
        if not isinstance(token_env, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", token_env
        ):
            print("[gitlab] invalid token_env", file=sys.stderr)
            return [], watermark
        token = os.environ.get(token_env, "")
        if (
            not token
            or len(token) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in token)
        ):
            return [], watermark
        gitlab_url = self._valid_base_url(config.get("gitlab_url", "https://gitlab.com"))
        if not gitlab_url:
            print("[gitlab] gitlab_url must be a credential-free HTTPS URL", file=sys.stderr)
            return [], watermark
        raw_username = config.get("username", "")
        fallback_username = config.get("_username", "")
        if not isinstance(raw_username, str) or not isinstance(fallback_username, str):
            print("[gitlab] username must be a string", file=sys.stderr)
            return [], watermark
        username = raw_username or fallback_username
        if (
            len(username) > 255
            or username != username.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in username)
        ):
            print("[gitlab] username is too long", file=sys.stderr)
            return [], watermark
        watch_reviews = config.get("watch_reviews", True)
        watch_pipelines = config.get("watch_pipelines", True)
        watch_todos = config.get("watch_todos", True)
        if any(
            type(value) is not bool
            for value in (watch_reviews, watch_pipelines, watch_todos)
        ):
            print("[gitlab] watch flags must be booleans", file=sys.stderr)
            return [], watermark
        per_page = config.get("max_items", 100)
        max_pages = config.get("max_pages", 3)
        projects_per_poll = config.get("projects_per_poll", 3)
        overlap = config.get("overlap_minutes", 10)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (per_page, max_pages, projects_per_poll, overlap)
            )
            or not 1 <= per_page <= 100
            or not 1 <= max_pages <= 5
            or not 1 <= projects_per_poll <= 10
            or not 0 <= overlap <= 1440
        ):
            print("[gitlab] pagination limits are invalid", file=sys.stderr)
            return [], watermark
        projects = config.get("watch_projects", [])
        if not isinstance(projects, list):
            return [], watermark
        if len(projects) > 100 or not all(
            isinstance(value, (str, int)) and not isinstance(value, bool)
            for value in projects
        ):
            return [], watermark
        if any(
            (isinstance(value, str) and (not value or value != value.strip()))
            or (isinstance(value, int) and value <= 0)
            for value in projects
        ):
            return [], watermark
        watched_projects = list(dict.fromkeys(str(value) for value in projects))
        if any(
            len(value) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in watched_projects
        ):
            return [], watermark

        state = self._load_state(watermark)
        if state is None:
            print("[gitlab] invalid persisted state; preserving watermark", file=sys.stderr)
            return [], watermark
        scan_started_at = self._utc_now_z()
        scope = hashlib.sha256(
            json.dumps(
                {
                    "gitlab_url": gitlab_url,
                    "username": username,
                    "projects": sorted(watched_projects),
                    "reviews": watch_reviews,
                    "pipelines": watch_pipelines,
                    "todos": watch_todos,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]
        same_scope = state.get("scope") == scope
        initialized = bool(state.get("initialized")) and same_scope
        pollen: list[dict] = []
        made_progress = False
        previous_mrs = state.get("merge_requests", {}) if same_scope else {}
        if not isinstance(previous_mrs, dict):
            return [], watermark
        current_mrs: dict[str, str] = dict(previous_mrs)
        reviews_initialized = (
            bool(state.get("reviews_initialized", initialized)) if same_scope else False
        )

        if watch_reviews and username:
            mrs = self._paginate(
                "/merge_requests",
                {
                    "scope": "all",
                    "state": "opened",
                    "reviewer_username": username,
                    "order_by": "updated_at",
                    "sort": "asc",
                },
                token,
                gitlab_url,
                per_page,
                max_pages,
            )
            if mrs is not None:
                candidate_mrs: dict[str, str] = {}
                review_pollen: list[dict] = []
                malformed_reviews = False
                for mr in mrs:
                    numeric_iid = _positive_id(mr.get("iid"))
                    numeric_project_id = _positive_id(mr.get("project_id"))
                    updated = mr.get("updated_at")
                    title = mr.get("title")
                    web_url = mr.get("web_url")
                    author = mr.get("author")
                    if (
                        numeric_iid is None
                        or numeric_project_id is None
                        or not _bounded_text(updated, 128, allow_empty=False)
                        or not isinstance(title, str)
                        or len(title) > 100_000
                        or mr.get("state") != "opened"
                        or not _bounded_text(web_url, 2_000)
                        or not _valid_person(author)
                    ):
                        malformed_reviews = True
                        break
                    iid = str(numeric_iid)
                    project_id = str(numeric_project_id)
                    mr_key = f"{project_id}:{iid}"
                    if mr_key in candidate_mrs:
                        malformed_reviews = True
                        break
                    candidate_mrs[mr_key] = updated
                    if not reviews_initialized or mr_key in previous_mrs:
                        continue
                    update_hash = hashlib.sha256(updated.encode()).hexdigest()[:8]
                    review_pollen.append({
                        "id": f"gitlab-mr-{project_id}-{iid}-{update_hash}",
                        "source": "gitlab",
                        "type": "gitlab_mr_review",
                        "title": title[:100],
                        "preview": f"MR !{iid}: {title}"[:200],
                        "discovered_at": scan_started_at,
                        "author": author.get("username", ""),
                        "author_name": author.get("name", ""),
                        "group": "Merge Requests",
                        "url": web_url,
                        "metadata": {
                            "iid": iid,
                            "state": "opened",
                            "project_id": project_id,
                            "updated_at": updated,
                        },
                    })
                if malformed_reviews:
                    print("[gitlab] malformed merge-request response", file=sys.stderr)
                else:
                    current_mrs = candidate_mrs
                    reviews_initialized = True
                    pollen.extend(review_pollen)
                    made_progress = True
        elif not watch_reviews:
            current_mrs = {}
            reviews_initialized = False

        raw_project_times = state.get("project_updated_at", {}) if same_scope else {}
        if not isinstance(raw_project_times, dict):
            return [], watermark
        project_updated_at = {
            project: str(value)
            for project, value in raw_project_times.items()
            if project in watched_projects and isinstance(value, str) and value
        }
        legacy_project_state = (
            initialized
            and not project_updated_at
            and bool(state.get("_legacy_project_scheduler"))
        )
        try:
            project_next_index = (
                int(state.get("project_next_index", 0) or 0) % len(watched_projects)
                if watched_projects
                else 0
            )
        except (TypeError, ValueError):
            project_next_index = 0
        initial_project_next_index = project_next_index
        if watch_pipelines:
            selected_projects = [
                watched_projects[(project_next_index + offset) % len(watched_projects)]
                for offset in range(min(projects_per_poll, len(watched_projects)))
            ] if watched_projects else []
            for project in selected_projects:
                baseline = project_updated_at.get(project, "")
                project_initialized = bool(baseline)
                if not baseline and legacy_project_state:
                    baseline = str(
                        state.get("updated_at") or "1970-01-01T00:00:00Z"
                    )
                    project_initialized = True
                if project_initialized:
                    try:
                        effective_since = (
                            datetime.fromisoformat(baseline.replace("Z", "+00:00"))
                            - timedelta(minutes=overlap)
                        ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    except (TypeError, ValueError):
                        print(
                            f"[gitlab] invalid saved pipeline cursor for {project}",
                            file=sys.stderr,
                        )
                        continue
                else:
                    effective_since = scan_started_at
                encoded_project = urllib.parse.quote(project, safe="")
                pipelines = self._paginate(
                    f"/projects/{encoded_project}/pipelines",
                    {
                        "updated_after": effective_since,
                        "status": "failed",
                        "order_by": "updated_at",
                        "sort": "asc",
                    },
                    token,
                    gitlab_url,
                    per_page,
                    max_pages,
                )
                if pipelines is None:
                    continue
                parsed_pipelines: list[tuple[str, str, str, str]] = []
                seen_pipeline_ids: set[str] = set()
                for pipeline in pipelines:
                    numeric_pipeline_id = _positive_id(pipeline.get("id"))
                    pipeline_id = (
                        str(numeric_pipeline_id)
                        if numeric_pipeline_id is not None
                        else ""
                    )
                    ref = pipeline.get("ref", "")
                    updated_at = pipeline.get("updated_at")
                    web_url = pipeline.get("web_url")
                    if (
                        not pipeline_id
                        or pipeline_id in seen_pipeline_ids
                        or pipeline.get("status") != "failed"
                        or not _bounded_text(ref, 1_000)
                        or not _bounded_text(updated_at, 128, allow_empty=False)
                        or not _bounded_text(web_url, 2_000)
                    ):
                        parsed_pipelines = []
                        break
                    seen_pipeline_ids.add(pipeline_id)
                    parsed_pipelines.append((pipeline_id, ref, updated_at, web_url))
                if len(parsed_pipelines) != len(pipelines):
                    print(
                        f"[gitlab] malformed failed-pipeline response for {project}",
                        file=sys.stderr,
                    )
                    continue
                project_updated_at[project] = scan_started_at
                made_progress = True
                if not project_initialized:
                    continue
                for pipeline_id, ref, updated_at, web_url in parsed_pipelines:
                    project_key = hashlib.sha256(project.encode()).hexdigest()[:12]
                    pollen.append({
                        "id": f"gitlab-ci-{project_key}-{pipeline_id}",
                        "source": "gitlab",
                        "type": "gitlab_ci_failure",
                        "title": f"Pipeline #{pipeline_id} failed in {project}"[:100],
                        "preview": f"Failed pipeline #{pipeline_id} on ref {ref}"[:200],
                        "discovered_at": scan_started_at,
                        "author": "",
                        "author_name": "",
                        "group": "CI Pipelines",
                        "url": web_url,
                        "metadata": {
                            "pipeline_id": pipeline_id,
                            "project_id": project,
                            "ref": ref,
                            "status": "failed",
                            "updated_at": updated_at,
                        },
                    })
            if watched_projects:
                project_next_index = (
                    project_next_index + len(selected_projects)
                ) % len(watched_projects)
        else:
            project_updated_at = {}
            project_next_index = 0

        todo_max_id = state.get("todo_max_id", 0) if same_scope else 0
        next_todo_max_id = todo_max_id
        todos_initialized = (
            bool(state.get("todos_initialized", initialized)) if same_scope else False
        )
        todo_set_initialized = (
            bool(state.get("todo_set_initialized"))
            if same_scope and state.get("version") == 5
            else False
        )
        previous_todo_ids = (
            {_positive_id(value) for value in state.get("todo_ids", [])}
            if same_scope and todo_set_initialized
            else set()
        )
        next_todo_ids = sorted(value for value in previous_todo_ids if value is not None)
        if watch_todos:
            todos = self._paginate(
                "/todos",
                {"state": "pending"},
                token,
                gitlab_url,
                per_page,
                max_pages,
            )
            if todos is not None:
                todo_pollen: list[dict] = []
                candidate_todo_max_id = todo_max_id
                malformed_todos = False
                seen_todo_ids: set[int] = set()
                for todo in todos:
                    numeric_todo_id = _positive_id(todo.get("id"))
                    body = todo.get("body", "")
                    action_name = todo.get("action_name", "")
                    target_type = todo.get("target_type", "")
                    target_url = todo.get("target_url", "")
                    author = todo.get("author")
                    target = todo.get("target")
                    if target is None:
                        target = {}
                    if (
                        numeric_todo_id is None
                        or numeric_todo_id in seen_todo_ids
                        or not isinstance(body, str)
                        or len(body) > 100_000
                        or not _bounded_text(action_name, 255)
                        or not _bounded_text(target_type, 255)
                        or not _bounded_text(target_url, 2_000)
                        or not _valid_person(author)
                        or not isinstance(target, dict)
                        or not _bounded_text(target.get("title", ""), 100_000)
                    ):
                        malformed_todos = True
                        break
                    seen_todo_ids.add(numeric_todo_id)
                    todo_id = str(numeric_todo_id)
                    candidate_todo_max_id = max(
                        candidate_todo_max_id, numeric_todo_id
                    )
                    is_new_todo = (
                        numeric_todo_id not in previous_todo_ids
                        if todo_set_initialized
                        else numeric_todo_id > todo_max_id
                    )
                    if not todos_initialized or not is_new_todo:
                        continue
                    todo_pollen.append({
                        "id": f"gitlab-todo-{todo_id}",
                        "source": "gitlab",
                        "type": "gitlab_mention",
                        "title": (target.get("title") or body)[:100],
                        "preview": body[:200],
                        "discovered_at": scan_started_at,
                        "author": author.get("username", ""),
                        "author_name": author.get("name", ""),
                        "group": "Mentions",
                        "url": target_url,
                        "metadata": {
                            "todo_id": todo_id,
                            "action_name": action_name,
                            "target_type": target_type,
                        },
                    })
                if malformed_todos:
                    print("[gitlab] malformed todo response", file=sys.stderr)
                else:
                    next_todo_max_id = candidate_todo_max_id
                    next_todo_ids = sorted(seen_todo_ids)
                    todos_initialized = True
                    todo_set_initialized = True
                    pollen.extend(todo_pollen)
                    made_progress = True
        else:
            next_todo_max_id = 0
            next_todo_ids = []
            todos_initialized = False
            todo_set_initialized = False

        if not made_progress and project_next_index == initial_project_next_index:
            return [], watermark
        next_state = {
            "version": 5,
            "updated_at": scan_started_at,
            "initialized": True,
            "scope": scope,
            "merge_requests": current_mrs,
            "todo_max_id": next_todo_max_id,
            "todo_ids": next_todo_ids,
            "todo_set_initialized": todo_set_initialized,
            "project_updated_at": project_updated_at,
            "project_next_index": project_next_index,
            "reviews_initialized": reviews_initialized,
            "todos_initialized": todos_initialized,
        }
        return pollen, self._dump_state(next_state)


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = GitLabScanner()
    if data.get("command") == "poll":
        result_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
