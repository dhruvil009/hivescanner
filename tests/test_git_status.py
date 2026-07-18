"""Functional tests for the local Git worktree scanner."""

import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
_PATH = os.path.join(os.path.dirname(__file__), "..", "workers", "sources", "git_status.py")
_spec = importlib.util.spec_from_file_location("git_status", _PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["git_status"] = _mod
_spec.loader.exec_module(_mod)
GitStatusScanner = _mod.GitStatusScanner


@pytest.fixture
def scanner():
    with patch.object(_mod, "load_snapshot", return_value={}):
        return GitStatusScanner()


@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-C", str(tmp_path), "-c", "user.name=Hive Test",
            "-c", "user.email=hive@example.test", "commit", "--allow-empty",
            "-m", "init",
        ],
        check=True,
        capture_output=True,
    )
    return tmp_path


def _config(repo, **changes):
    return {
        "enabled": True,
        "watch_dirs": [str(repo)],
        "warn_uncommitted_after_minutes": 0,
        "warn_branch_behind": True,
        **changes,
    }


def _poll(scanner, config, watermark="1970-01-01T00:00:00Z"):
    with patch.object(_mod, "save_snapshot"):
        return scanner.poll(config, watermark)


def test_configure_watches_current_repo_by_default(scanner):
    config = scanner.configure()
    assert config["enabled"] is True
    assert config["watch_dirs"] == ["."]
    assert config["warn_uncommitted_after_minutes"] == 60
    assert config["repos_per_poll"] == 5


def test_clean_repo_has_no_dirty_or_conflict_warning(scanner, git_repo):
    pollen, _ = _poll(scanner, _config(git_repo))
    assert not {"uncommitted_warning", "merge_conflict"} & {
        item["type"] for item in pollen
    }


def test_dirty_repo_warns_after_configured_age(scanner, git_repo):
    (git_repo / "dirty.txt").write_text("dirty")
    pollen, _ = _poll(scanner, _config(git_repo))
    warning = next(item for item in pollen if item["type"] == "uncommitted_warning")
    assert warning["source"] == "git_status"
    assert warning["metadata"]["file_count"] == 1
    assert warning["metadata"]["dir"] == str(git_repo.resolve())


def test_default_age_does_not_immediately_warn_new_dirty_worktree(scanner, git_repo):
    (git_repo / "dirty.txt").write_text("dirty")
    pollen, _ = _poll(
        scanner,
        _config(git_repo, warn_uncommitted_after_minutes=60),
    )
    assert not any(item["type"] == "uncommitted_warning" for item in pollen)


def test_dirty_episode_id_survives_edits_and_status_hash_changes(scanner, git_repo):
    dirty = git_repo / "dirty.txt"
    dirty.write_text("one")
    first, _ = _poll(scanner, _config(git_repo))
    first_warning = next(item for item in first if item["type"] == "uncommitted_warning")

    dirty.write_text("two")
    (git_repo / "another.txt").write_text("new")
    second, _ = _poll(scanner, _config(git_repo))
    second_warning = next(item for item in second if item["type"] == "uncommitted_warning")

    assert first_warning["id"] == second_warning["id"]
    assert first_warning["metadata"]["state_hash"] != second_warning["metadata"]["state_hash"]


def test_existing_dirty_age_is_preserved(scanner, git_repo):
    (git_repo / "dirty.txt").write_text("dirty")
    scanner._uncommitted_snapshot[str(git_repo.resolve())] = {
        "state_hash": "old",
        "episode_id": "0123456789abcdef",
        "first_seen": (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    pollen, _ = _poll(
        scanner,
        _config(git_repo, warn_uncommitted_after_minutes=60),
    )
    warning = next(item for item in pollen if item["type"] == "uncommitted_warning")
    assert warning["id"].endswith("-0123456789abcdef")
    assert warning["metadata"]["observed_age_minutes"] >= 119


def test_stash_is_reported(scanner, git_repo):
    (git_repo / "stash_me.txt").write_text("stash")
    subprocess.run(["git", "-C", str(git_repo), "add", "stash_me.txt"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "stash"], check=True, capture_output=True)
    pollen, _ = _poll(scanner, _config(git_repo))
    reminder = next(item for item in pollen if item["type"] == "stash_reminder")
    assert reminder["metadata"]["stash_count"] == 1


def test_non_repo_and_invalid_watch_list_are_safe(scanner, tmp_path):
    assert _poll(scanner, _config(tmp_path))[0] == []
    watermark = "2026-07-15T00:00:00Z"
    assert _poll(scanner, {"watch_dirs": " " * 5000}, watermark)[0] == []
    assert _poll(scanner, {"watch_dirs": ["x"] * 101}, watermark) == ([], watermark)


def test_clean_worktree_clears_the_previous_dirty_episode(scanner, git_repo):
    key = str(git_repo.resolve())
    scanner._uncommitted_snapshot[key] = {
        "state_hash": "old",
        "episode_id": "0123456789abcdef",
        "first_seen": "2026-07-15T00:00:00Z",
    }
    _poll(scanner, _config(git_repo))
    assert key not in scanner._uncommitted_snapshot


def test_invalid_config_values_fail_closed(scanner, git_repo):
    watermark = "2026-07-15T00:00:00Z"
    assert _poll(
        scanner,
        _config(git_repo, warn_branch_behind="false"),
        watermark,
    ) == ([], watermark)
    assert _poll(
        scanner,
        _config(git_repo, warn_uncommitted_after_minutes=-1),
        watermark,
    ) == ([], watermark)
    assert _poll(
        scanner,
        _config(git_repo, repos_per_poll=0),
        watermark,
    ) == ([], watermark)


def test_nonnumeric_behind_count_is_ignored_instead_of_crashing(scanner, git_repo):
    def fake(args, cwd="."):
        if args[0] == "rev-parse":
            return ".git\n"
        if args[0] == "status":
            return ""
        if args[0] == "rev-list":
            return "not-a-count\n"
        return ""

    with patch.object(scanner, "_git", side_effect=fake):
        pollen, _ = _poll(scanner, _config(git_repo))
    assert not any(item["type"] == "branch_behind" for item in pollen)


def test_repo_rotation_bounds_work_per_poll_and_preserves_progress(scanner, tmp_path):
    repos = []
    for index in range(6):
        repo = tmp_path / f"repo-{index}"
        repo.mkdir()
        repos.append(str(repo))
    visited = []

    def fake(args, cwd="."):
        if args[0] == "rev-parse":
            visited.append(cwd)
            return ".git\n"
        return ""

    config = {
        **scanner.configure(),
        "watch_dirs": repos,
        "repos_per_poll": 2,
    }
    with patch.object(scanner, "_git", side_effect=fake):
        _poll(scanner, config)
        assert visited == repos[:2]
        visited.clear()
        _poll(scanner, config)
    assert visited == repos[2:4]


def test_parent_git_environment_cannot_redirect_repository_commands(scanner, git_repo):
    with patch.dict(os.environ, {"GIT_DIR": "/definitely/not/the/repo"}):
        assert scanner._git(["rev-parse", "--git-dir"], cwd=str(git_repo)) is not None


def test_repo_configured_fsmonitor_hook_is_disabled(scanner, git_repo):
    marker = git_repo / "fsmonitor-ran"
    hook = git_repo / "malicious-fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(git_repo), "config", "core.fsmonitor", str(hook)],
        check=True,
    )
    _poll(scanner, _config(git_repo))
    assert not marker.exists()


def test_unknown_or_malformed_snapshot_fails_closed_without_crashing(tmp_path):
    invalid_states = [
        {"schema_version": []},
        {
            "schema_version": 2,
            "repos": {
                str(tmp_path): {
                    "state_hash": [],
                    "episode_id": "0123456789abcdef",
                    "first_seen": "2026-07-15T00:00:00Z",
                }
            },
            "next_index": 0,
        },
        {"schema_version": 99, "repos": {}, "next_index": 0},
    ]
    watermark = "2026-07-15T00:00:00Z"
    for stored in invalid_states:
        with patch.object(_mod, "load_snapshot", return_value=stored):
            subject = GitStatusScanner()
        with patch.object(subject, "_git") as git:
            assert _poll(subject, subject.configure(), watermark) == ([], watermark)
        git.assert_not_called()


def test_temporarily_missing_configured_directory_preserves_dirty_age(tmp_path):
    missing = tmp_path / "offline-volume" / "repo"
    key = str(missing.resolve())
    state = {
        "state_hash": "deadbeef",
        "episode_id": "0123456789abcdef",
        "first_seen": (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with patch.object(_mod, "load_snapshot", return_value={key: state}):
        subject = GitStatusScanner()
    _poll(subject, {**subject.configure(), "watch_dirs": [str(missing)]})
    assert subject._uncommitted_snapshot[key] == state
    assert subject._next_index == 0


def test_invalid_watermark_and_whitespace_path_fail_before_git(scanner):
    with patch.object(scanner, "_git") as git:
        assert scanner.poll(scanner.configure(), "not-a-time") == ([], "not-a-time")
        watermark = "2026-07-15T00:00:00Z"
        config = {**scanner.configure(), "watch_dirs": [" ./repo"]}
        assert scanner.poll(config, watermark) == ([], watermark)
    git.assert_not_called()


def test_git_output_must_be_utf8(scanner):
    def invalid_utf8(*args, **kwargs):
        kwargs["stdout"].write(b"\xff")
        return SimpleNamespace(returncode=0)

    with patch.object(_mod.subprocess, "run", side_effect=invalid_utf8):
        assert scanner._git(["status"]) is None
