"""Tests for git_status scanner."""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers", "sources"))
from git_status import GitStatusScanner


@pytest.fixture
def scanner():
    return GitStatusScanner()


@pytest.fixture
def git_repo(tmp_path):
    """Create a real temporary git repo."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
                   capture_output=True)
    return tmp_path


class TestGitStatusScanner:
    def test_configure(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is True
        assert "." in config["watch_dirs"]

    def test_poll_clean_repo(self, scanner, git_repo):
        config = {"enabled": True, "watch_dirs": [str(git_repo)],
                  "warn_uncommitted_after_minutes": 60, "warn_branch_behind": True}
        pollen, wm = scanner.poll(config, "1970-01-01T00:00:00Z")
        # Clean repo with no remote — should have no conflicts
        conflict_pollen = [p for p in pollen if p["type"] == "merge_conflict"]
        assert len(conflict_pollen) == 0

    def test_poll_uncommitted_changes(self, scanner, git_repo):
        # Create an uncommitted file
        (git_repo / "dirty.txt").write_text("dirty")
        config = {"enabled": True, "watch_dirs": [str(git_repo)],
                  "warn_uncommitted_after_minutes": 60, "warn_branch_behind": True}
        pollen, wm = scanner.poll(config, "1970-01-01T00:00:00Z")
        uncommitted = [p for p in pollen if p["type"] == "uncommitted_warning"]
        assert len(uncommitted) == 1
        assert uncommitted[0]["source"] == "git_status"

    def test_poll_stash(self, scanner, git_repo):
        # Create a file, stage it, stash it
        (git_repo / "stash_me.txt").write_text("stash")
        subprocess.run(["git", "-C", str(git_repo), "add", "stash_me.txt"], capture_output=True)
        subprocess.run(["git", "-C", str(git_repo), "stash"], capture_output=True)
        config = {"enabled": True, "watch_dirs": [str(git_repo)],
                  "warn_uncommitted_after_minutes": 60, "warn_branch_behind": True}
        pollen, wm = scanner.poll(config, "1970-01-01T00:00:00Z")
        stash_pollen = [p for p in pollen if p["type"] == "stash_reminder"]
        assert len(stash_pollen) == 1

    def test_poll_nonexistent_dir(self, scanner):
        config = {"enabled": True, "watch_dirs": ["/nonexistent/dir"],
                  "warn_uncommitted_after_minutes": 60, "warn_branch_behind": True}
        pollen, wm = scanner.poll(config, "1970-01-01T00:00:00Z")
        assert pollen == []
