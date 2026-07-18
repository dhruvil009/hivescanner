"""Git status scanner — zero-config, zero-network, zero-auth local git monitoring."""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from snapshot_store import load_snapshot, save_snapshot
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from snapshot_store import load_snapshot, save_snapshot


def _state_hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:8]


class GitStatusScanner:
    name = "git_status"
    _POLL_BUDGET_SECONDS = 45

    def __init__(self):
        stored = load_snapshot("git_status_uncommitted")
        self._state_valid = True
        if type(stored.get("schema_version")) is int and stored.get("schema_version") == 2:
            repos = stored.get("repos")
            next_index = stored.get("next_index")
            if (
                set(stored) != {"schema_version", "repos", "next_index"}
                or not self._valid_repos(repos)
                or isinstance(next_index, bool)
                or not isinstance(next_index, int)
                or not 0 <= next_index < 100
            ):
                self._state_valid = False
                repos, next_index = {}, 0
            self._uncommitted_snapshot = repos
            self._next_index = next_index
        elif "schema_version" in stored:
            self._state_valid = False
            self._uncommitted_snapshot = {}
            self._next_index = 0
        else:
            if not self._valid_repos(stored):
                self._state_valid = False
                stored = {}
            self._uncommitted_snapshot = stored
            self._next_index = 0

    def configure(self) -> dict:
        return {
            "enabled": True,
            "watch_dirs": ["."],
            "warn_uncommitted_after_minutes": 60,
            "warn_branch_behind": True,
            "repos_per_poll": 5,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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

    @classmethod
    def _valid_repos(cls, value: object) -> bool:
        if not isinstance(value, dict) or len(value) > 100:
            return False
        now = datetime.now(timezone.utc)
        for path, state in value.items():
            state_hash = state.get("state_hash") if isinstance(state, dict) else None
            episode_id = state.get("episode_id") if isinstance(state, dict) else None
            if (
                not isinstance(path, str)
                or not path
                or len(path) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or not isinstance(state, dict)
                or set(state) != {"state_hash", "episode_id", "first_seen"}
                or not isinstance(state_hash, str)
                or re.fullmatch(r"[0-9a-f]{8}", state_hash) is None
                or not isinstance(episode_id, str)
                or re.fullmatch(r"[0-9a-f]{16}", episode_id) is None
            ):
                return False
            first_seen = cls._parse_timestamp(state.get("first_seen"))
            if first_seen is None or first_seen > now + timedelta(minutes=5):
                return False
        return True

    def _git(self, args: list[str], cwd: str = ".") -> str | None:
        deadline = getattr(self, "_poll_deadline", None)
        timeout = 10.0
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            timeout = min(timeout, max(0.1, remaining))
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")
        }
        env.update({
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        })
        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(
                    [
                        "git",
                        "--no-optional-locks",
                        "-c", "core.fsmonitor=false",
                        "-c", "core.hooksPath=",
                        "-c", "diff.external=",
                        *args,
                    ],
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                    cwd=cwd,
                    env=env,
                )
                output.seek(0)
                raw = output.read(1_000_001)
            if result.returncode != 0:
                return None
            if len(raw) > 1_000_000:
                print("[git_status] git output exceeded 1 MB", file=sys.stderr)
                return None
            return raw.decode("utf-8")
        except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError, ValueError):
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        if not isinstance(config, dict) or not isinstance(watermark, str):
            return [], watermark
        if watermark and self._parse_timestamp(watermark) is None:
            print("[git_status] invalid watermark; preserving it", file=sys.stderr)
            return [], watermark
        if not self._state_valid:
            print("[git_status] invalid persisted snapshot; preserving watermark", file=sys.stderr)
            return [], watermark
        pollen = []
        watch_dirs = config.get("watch_dirs", ["."])
        if not isinstance(watch_dirs, list):
            print("[git_status] watch_dirs must be a list", file=sys.stderr)
            return [], watermark
        if len(watch_dirs) > 100:
            print("[git_status] watch_dirs exceeds the limit of 100", file=sys.stderr)
            return [], watermark
        if any(
            not isinstance(watch_dir, str)
            or not watch_dir
            or watch_dir != watch_dir.strip()
            or len(watch_dir) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in watch_dir)
            for watch_dir in watch_dirs
        ):
            print("[git_status] watch_dirs contains an invalid path", file=sys.stderr)
            return [], watermark
        warn_minutes = config.get("warn_uncommitted_after_minutes", 60)
        if (
            isinstance(warn_minutes, bool)
            or not isinstance(warn_minutes, (int, float))
            or not math.isfinite(warn_minutes)
            or not 0 <= warn_minutes <= 525_600
        ):
            print("[git_status] invalid uncommitted warning age", file=sys.stderr)
            return [], watermark
        warn_behind = config.get("warn_branch_behind", True)
        if not isinstance(warn_behind, bool):
            print("[git_status] warn_branch_behind must be a boolean", file=sys.stderr)
            return [], watermark
        repos_per_poll = config.get("repos_per_poll", 5)
        if (
            isinstance(repos_per_poll, bool)
            or not isinstance(repos_per_poll, int)
            or not 1 <= repos_per_poll <= 20
        ):
            print("[git_status] repos_per_poll must be an integer from 1 to 20", file=sys.stderr)
            return [], watermark

        configured_dirs: list[str] = []
        resolved_dirs: list[str] = []
        seen_dirs: set[str] = set()
        for watch_dir in watch_dirs:
            try:
                expanded = os.path.expanduser(watch_dir)
                resolved = str(Path(expanded).resolve())
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved not in seen_dirs:
                seen_dirs.add(resolved)
                configured_dirs.append(resolved)
                if os.path.isdir(resolved):
                    resolved_dirs.append(resolved)

        active_dirs = set(configured_dirs)
        next_uncommitted_snapshot = {
            path: value
            for path, value in self._uncommitted_snapshot.items()
            if path in active_dirs and isinstance(value, dict)
        }
        now = datetime.now(timezone.utc)
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS

        if resolved_dirs:
            start_index = self._next_index % len(resolved_dirs)
            selected_count = min(repos_per_poll, len(resolved_dirs))
            selected_dirs = [
                resolved_dirs[(start_index + offset) % len(resolved_dirs)]
                for offset in range(selected_count)
            ]
            next_index = (start_index + selected_count) % len(resolved_dirs)
        else:
            selected_dirs = []
            next_index = 0

        for resolved_dir in selected_dirs:
            # Check if it's a git repo
            if self._git(["rev-parse", "--git-dir"], cwd=resolved_dir) is None:
                continue

            dir_label = os.path.basename(resolved_dir) or resolved_dir
            dir_key = hashlib.sha256(resolved_dir.encode()).hexdigest()[:12]

            # --- Uncommitted changes ---
            status = self._git(
                [
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=normal",
                    "--ignore-submodules=all",
                ],
                cwd=resolved_dir,
            )
            if status is None:
                # A timeout or oversized repository must not reset the dirty
                # episode's age and cause a fresh warning later.
                prior = self._uncommitted_snapshot.get(resolved_dir)
                if isinstance(prior, dict):
                    next_uncommitted_snapshot[resolved_dir] = prior
            elif status:
                raw_entries = [entry for entry in status.split("\0") if entry]
                if not raw_entries:
                    prior = self._uncommitted_snapshot.get(resolved_dir)
                    if isinstance(prior, dict):
                        next_uncommitted_snapshot[resolved_dir] = prior
                    continue
                lines = []
                index = 0
                status_valid = True
                while index < len(raw_entries):
                    entry = raw_entries[index]
                    if len(entry) < 4 or entry[2] != " ":
                        status_valid = False
                        break
                    lines.append(entry)
                    if len(entry) >= 2 and ("R" in entry[:2] or "C" in entry[:2]):
                        if index + 1 >= len(raw_entries):
                            status_valid = False
                            break
                        index += 1  # porcelain -z stores the old rename path next
                    index += 1
                if not status_valid:
                    prior = self._uncommitted_snapshot.get(resolved_dir)
                    if isinstance(prior, dict):
                        next_uncommitted_snapshot[resolved_dir] = prior
                    continue
                if lines:
                    state_hash = _state_hash(status)
                    prior = self._uncommitted_snapshot.get(resolved_dir, {})
                    # The age is how long the worktree has continuously been
                    # dirty, not how long the exact current diff has existed.
                    if not isinstance(prior, dict):
                        first_seen = now
                    else:
                        try:
                            first_seen = datetime.fromisoformat(
                                str(prior.get("first_seen", "")).replace("Z", "+00:00")
                            )
                            if first_seen.tzinfo is None:
                                raise ValueError
                            first_seen = first_seen.astimezone(timezone.utc)
                            if first_seen > now + timedelta(minutes=5):
                                raise ValueError
                        except (ValueError, TypeError, OverflowError):
                            first_seen = now
                    episode_id = (
                        str(prior.get("episode_id") or "")
                        if isinstance(prior, dict)
                        else ""
                    )
                    if not re.fullmatch(r"[a-f0-9]{16}", episode_id):
                        episode_id = uuid.uuid4().hex[:16]
                    next_uncommitted_snapshot[resolved_dir] = {
                        "state_hash": state_hash,
                        "episode_id": episode_id,
                        "first_seen": first_seen.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                    age_minutes = max(0, (now - first_seen).total_seconds() / 60)
                    if age_minutes >= warn_minutes:
                        pollen_id = f"git-uncommitted-{dir_key}-{episode_id}"
                        pollen.append({
                            "id": pollen_id,
                            "source": "git_status",
                            "type": "uncommitted_warning",
                            "title": f"{len(lines)} uncommitted changes in {dir_label}",
                            "preview": f"{len(lines)} modified/untracked files in {dir_label}",
                            "discovered_at": self._utc_now_z(),
                            "author": "",
                            "author_name": "",
                            "group": "Git",
                            "url": "",
                            "metadata": {
                                "dir": resolved_dir,
                                "file_count": len(lines),
                                "files": [line[3:] if len(line) > 3 else line for line in lines[:5]],
                                "observed_age_minutes": round(age_minutes, 1),
                                "state_hash": state_hash,
                            },
                        })
            else:
                next_uncommitted_snapshot.pop(resolved_dir, None)

            # --- Branch behind remote ---
            if warn_behind:
                behind = self._git(
                    ["rev-list", "--count", "HEAD..@{u}"], cwd=resolved_dir
                )
                raw_count = behind.strip() if behind else ""
                if raw_count and re.fullmatch(r"\d{1,18}", raw_count) is None:
                    print(
                        f"[git_status] invalid behind count for {resolved_dir}",
                        file=sys.stderr,
                    )
                elif raw_count and raw_count != "0":
                    count = int(raw_count)
                    branch = self._git(
                        ["branch", "--show-current"], cwd=resolved_dir
                    )
                    branch_name = (
                        branch.strip()
                        if branch and branch.strip()
                        else "detached HEAD"
                    )
                    if (
                        len(branch_name) > 200
                        or any(
                            ord(char) < 32 or ord(char) == 127
                            for char in branch_name
                        )
                    ):
                        branch_name = "current branch"
                    pollen_id = f"git-behind-{dir_key}-{_state_hash(f'{branch_name}:{count}')}"
                    pollen.append({
                        "id": pollen_id,
                        "source": "git_status",
                        "type": "branch_behind",
                        "title": f"{branch_name} is {count} commits behind remote",
                        "preview": f"Branch {branch_name} in {dir_label} is {count} commits behind upstream",
                        "discovered_at": self._utc_now_z(),
                        "author": "",
                        "author_name": "",
                        "group": "Git",
                        "url": "",
                        "metadata": {
                            "dir": resolved_dir,
                            "branch": branch_name,
                            "behind_count": count,
                        },
                    })

            # --- Stash entries ---
            stash = self._git(["stash", "list"], cwd=resolved_dir)
            if stash and stash.strip():
                stash_lines = stash.strip().split("\n")
                pollen_id = f"git-stash-{dir_key}-{_state_hash(stash)}"
                pollen.append({
                    "id": pollen_id,
                    "source": "git_status",
                    "type": "stash_reminder",
                    "title": f"{len(stash_lines)} stash entries in {dir_label}",
                    "preview": f"You have {len(stash_lines)} stashed changes in {dir_label}",
                    "discovered_at": self._utc_now_z(),
                    "author": "",
                    "author_name": "",
                    "group": "Git",
                    "url": "",
                    "metadata": {
                        "dir": resolved_dir,
                        "stash_count": len(stash_lines),
                        "entries": stash_lines[:3],
                    },
                })

            # --- Merge conflicts ---
            conflicts = self._git(
                ["diff", "--name-only", "-z", "--diff-filter=U"],
                cwd=resolved_dir,
            )
            if conflicts and conflicts.strip():
                conflict_files = [name for name in conflicts.split("\0") if name]
                pollen_id = f"git-conflict-{dir_key}-{_state_hash(conflicts)}"
                pollen.append({
                    "id": pollen_id,
                    "source": "git_status",
                    "type": "merge_conflict",
                    "title": f"Merge conflicts in {dir_label}",
                    "preview": f"{len(conflict_files)} files with merge conflicts in {dir_label}",
                    "discovered_at": self._utc_now_z(),
                    "author": "",
                    "author_name": "",
                    "group": "Git",
                    "url": "",
                    "metadata": {
                        "dir": resolved_dir,
                        "conflict_files": conflict_files[:5],
                    },
                })

        self._uncommitted_snapshot = next_uncommitted_snapshot
        self._next_index = next_index
        save_snapshot("git_status_uncommitted", {
            "schema_version": 2,
            "repos": self._uncommitted_snapshot,
            "next_index": self._next_index,
        })
        return pollen, self._utc_now_z()
