"""Git status scanner — zero-config, zero-network, zero-auth local git monitoring."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _state_hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:8]


class GitStatusScanner:
    name = "git_status"

    def configure(self) -> dict:
        return {
            "enabled": True,
            "watch_dirs": ["."],
            "warn_uncommitted_after_minutes": 60,
            "warn_branch_behind": True,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _git(self, args: list[str], cwd: str = ".") -> str | None:
        try:
            result = subprocess.run(
                ["git"] + args,
                capture_output=True, text=True, timeout=10, cwd=cwd,
            )
            if result.returncode != 0:
                return None
            return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        pollen = []
        watch_dirs = config.get("watch_dirs", ["."])
        warn_minutes = config.get("warn_uncommitted_after_minutes", 60)
        warn_behind = config.get("warn_branch_behind", True)

        for watch_dir in watch_dirs:
            d = os.path.expanduser(watch_dir)
            if not os.path.isdir(d):
                continue

            # Check if it's a git repo
            if self._git(["rev-parse", "--git-dir"], cwd=d) is None:
                continue

            dir_label = os.path.basename(os.path.abspath(d)) or d

            # --- Uncommitted changes ---
            status = self._git(["status", "--porcelain"], cwd=d)
            if status and status.strip():
                lines = [l for l in status.strip().split("\n") if l.strip()]
                if lines:
                    pollen_id = f"git-uncommitted-{dir_label}-{_state_hash(status)}"
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
                            "dir": d,
                            "file_count": len(lines),
                            "files": [l[3:] if len(l) > 3 else l for l in lines[:5]],
                        },
                    })

            # --- Branch behind remote ---
            if warn_behind:
                behind = self._git(["rev-list", "--count", "HEAD..@{u}"], cwd=d)
                if behind and behind.strip() and behind.strip() != "0":
                    count = behind.strip()
                    branch = self._git(["branch", "--show-current"], cwd=d)
                    branch_name = branch.strip() if branch else "current branch"
                    pollen_id = f"git-behind-{dir_label}-{branch_name}-{count}"
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
                            "dir": d,
                            "branch": branch_name,
                            "behind_count": int(count),
                        },
                    })

            # --- Stash entries ---
            stash = self._git(["stash", "list"], cwd=d)
            if stash and stash.strip():
                stash_lines = stash.strip().split("\n")
                pollen_id = f"git-stash-{dir_label}-{_state_hash(stash)}"
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
                        "dir": d,
                        "stash_count": len(stash_lines),
                        "entries": stash_lines[:3],
                    },
                })

            # --- Merge conflicts ---
            conflicts = self._git(["diff", "--name-only", "--diff-filter=U"], cwd=d)
            if conflicts and conflicts.strip():
                conflict_files = conflicts.strip().split("\n")
                pollen_id = f"git-conflict-{dir_label}-{_state_hash(conflicts)}"
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
                        "dir": d,
                        "conflict_files": conflict_files[:5],
                    },
                })

        return pollen, self._utc_now_z()
