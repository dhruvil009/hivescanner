"""Functional tests for GitHub notifications, CI, and acted checks."""

import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
from sources import github as _mod  # noqa: E402

GitHubScanner = _mod.GitHubScanner


NOTIFICATIONS = [
    {
        "id": "notif-pr-review",
        "updated_at": "2026-07-15T10:00:00Z",
        "reason": "review_requested",
        "subject": {
            "title": "Add new feature",
            "type": "PullRequest",
            "url": "https://api.github.com/repos/acme/widgets/pulls/123",
        },
        "repository": {"full_name": "acme/widgets"},
    },
    {
        "id": "notif-pr-mention",
        "updated_at": "2026-07-15T10:05:00Z",
        "reason": "mention",
        "subject": {
            "title": "Please take a look",
            "type": "PullRequest",
            "url": "https://api.github.com/repos/acme/widgets/pulls/456",
        },
        "repository": {"full_name": "acme/widgets"},
    },
    {
        "id": "notif-issue-mention",
        "updated_at": "2026-07-15T10:10:00Z",
        "reason": "mention",
        "subject": {
            "title": "Bug in parser",
            "type": "Issue",
            "url": "https://api.github.com/repos/acme/widgets/issues/789",
        },
        "repository": {"full_name": "acme/widgets"},
    },
]


def _scanner(*, ready=True):
    with (
        patch.object(_mod, "load_snapshot", return_value={}),
        patch.object(_mod, "snapshot_exists", return_value=ready),
    ):
        scanner = GitHubScanner()
    scanner._cli_available = True
    return scanner


def test_pr_url_helpers_reject_non_pr_and_non_github_urls():
    assert GitHubScanner._pr_number_from_api_url(
        "https://api.github.com/repos/acme/widgets/pulls/123"
    ) == 123
    assert GitHubScanner._pr_number_from_api_url(
        "https://api.github.com/repos/acme/widgets/issues/123"
    ) is None
    assert _scanner()._api_url_to_web("https://evil.test/repos/a/b/pulls/1") == ""


def test_notifications_use_pagination_contract_and_carry_pr_identity():
    scanner = _scanner()
    with patch.object(scanner, "_gh", return_value=json.dumps(NOTIFICATIONS)) as gh:
        items = scanner._poll_notifications(
            scanner.configure(), "2026-07-15T09:00:00Z", False
        )

    review = next(item for item in items if item["type"] == "review_needed")
    assert review["metadata"]["repo"] == "acme/widgets"
    assert review["metadata"]["pr_number"] == 123
    assert review["metadata"]["notification_updated_at"] == "2026-07-15T10:00:00Z"
    assert review["url"] == "https://github.com/acme/widgets/pull/123"
    assert "notif-pr-review" in review["id"]
    args = gh.call_args.args[0]
    assert args[:4] == ["api", "--method", "GET", "/notifications"]
    assert "since=2026-07-15T08:59:00Z" in args


def test_notification_controls_and_repo_filter_are_respected():
    scanner = _scanner()
    config = {
        **scanner.configure(),
        "watch_repos": ["other/repo"],
        "watch_mentions": False,
    }
    with patch.object(scanner, "_gh", return_value=json.dumps(NOTIFICATIONS)):
        assert scanner._poll_notifications(config, "2026-07-15T09:00:00Z", False) == []


def test_notification_page_limit_fails_closed_instead_of_skipping_backlog():
    scanner = _scanner()
    config = {
        **scanner.configure(),
        "max_items_per_query": 1,
        "max_notification_pages": 1,
    }
    with patch.object(scanner, "_gh", return_value=json.dumps([NOTIFICATIONS[0]])):
        try:
            scanner._poll_notifications(config, "2026-07-15T09:00:00Z", False)
        except RuntimeError as exc:
            assert "exceeded" in str(exc)
        else:
            raise AssertionError("a truncated notification stream must fail closed")


def test_first_full_poll_is_quiet_and_stages_bootstrap_marker():
    scanner = _scanner(ready=False)
    config = {**scanner.configure(), "watch_ci": False}
    with (
        patch.object(scanner, "_gh", return_value=json.dumps(NOTIFICATIONS)),
        patch.object(_mod, "save_snapshot"),
    ):
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    assert scanner._snapshot == {
        "schema_version": 3,
        "committed": {"boundary": "", "bootstrapped": False},
        "candidate": {"boundary": watermark, "bootstrapped": True},
        "candidate_watermark": watermark,
    }


def test_notification_failure_does_not_rollback_ci_component():
    scanner = _scanner()
    scanner._snapshot = {
        "schema_version": 3,
        "committed": {"boundary": "2026-07-15T09:00:00Z", "bootstrapped": True},
        "candidate": {"boundary": "2026-07-15T09:00:00Z", "bootstrapped": True},
        "candidate_watermark": "2026-07-15T09:00:00Z",
    }
    ci_item = {"id": "ci-item"}
    with (
        patch.object(scanner, "_poll_notifications", side_effect=RuntimeError("down")),
        patch.object(scanner, "_poll_ci_status", return_value=([ci_item], {"statuses": {}})),
        patch.object(_mod, "save_snapshot"),
    ):
        pollen, watermark = scanner.poll(scanner.configure(), "2026-07-15T10:00:00Z")

    assert pollen == [ci_item]
    assert watermark != "2026-07-15T10:00:00Z"
    committed = scanner._prepare_notification_state(scanner._snapshot, watermark, True)
    assert committed["boundary"] == "2026-07-15T09:00:00Z"
    assert scanner._pr_status_snapshot["candidate"] == {"statuses": {}}


def test_ci_failure_does_not_rollback_notification_component():
    scanner = _scanner()
    notification_item = {"id": "notification-item"}
    with (
        patch.object(scanner, "_poll_notifications", return_value=[notification_item]),
        patch.object(scanner, "_poll_ci_status", side_effect=RuntimeError("down")),
        patch.object(_mod, "save_snapshot"),
    ):
        pollen, watermark = scanner.poll(scanner.configure(), "2026-07-15T10:00:00Z")

    assert pollen == [notification_item]
    committed = scanner._prepare_notification_state(scanner._snapshot, watermark, True)
    assert committed["boundary"] == watermark


def test_ci_keys_include_repository_when_pr_numbers_collide():
    scanner = _scanner()
    payload = {
        "data": {
            "viewer": {
                "login": "alice",
                "pullRequests": {
                    "nodes": [
                        {
                            "number": 7,
                            "title": "A",
                            "headRefOid": "a" * 40,
                            "repository": {"nameWithOwner": "acme/a"},
                            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]},
                        },
                        {
                            "number": 7,
                            "title": "B",
                            "headRefOid": "b" * 40,
                            "repository": {"nameWithOwner": "acme/b"},
                            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]},
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }
    }
    with patch.object(scanner, "_gh", return_value=json.dumps(payload)):
        items, state = scanner._poll_ci_status("alice", [], {}, False, scanner.configure())
    assert len(items) == 2
    assert set(state["statuses"]) == {"acme/a#7", "acme/b#7"}
    assert len({item["id"] for item in items}) == 2


def test_check_acted_reads_paginated_review_objects_case_insensitively():
    scanner = _scanner()
    pollen = {
        "type": "review_needed",
        "metadata": {
            "repo": "acme/widgets",
            "pr_number": 123,
            "notification_updated_at": "2026-07-15T10:00:00Z",
        },
    }
    reviews = [[{
        "user": {"login": "alice"},
        "submitted_at": "2026-07-15T10:01:00+00:00",
    }]]
    with patch.object(scanner, "_gh", return_value=json.dumps(reviews)) as gh:
        assert scanner.check_acted(pollen, {"username": "Alice"}) is True
    args = gh.call_args.args[0]
    assert "--paginate" in args and "--slurp" in args


def test_check_acted_rejects_old_review_and_untrusted_repo_path():
    scanner = _scanner()
    base = {
        "type": "review_needed",
        "metadata": {
            "repo": "acme/widgets",
            "pr_number": 123,
            "notification_updated_at": "2026-07-15T10:00:00Z",
        },
    }
    old = [{
        "user": {"login": "alice"},
        "submitted_at": "2026-07-15T09:59:59Z",
    }]
    with patch.object(scanner, "_gh", return_value=json.dumps(old)):
        assert scanner.check_acted(base, {"username": "alice"}) is False

    poisoned = {**base, "metadata": {"repo": "../evil", "pr_number": 1}}
    with patch.object(scanner, "_gh") as gh:
        assert scanner.check_acted(poisoned, {"username": "alice"}) is False
        gh.assert_not_called()


def test_invalid_current_snapshots_and_config_fail_before_cli_calls():
    watermark = "2026-07-15T10:00:00Z"
    scanner = _scanner()
    scanner._snapshot = {
        "schema_version": 3,
        "committed": {"boundary": watermark, "bootstrapped": True},
        "candidate": {"boundary": watermark, "bootstrapped": "true"},
        "candidate_watermark": watermark,
    }
    with patch.object(scanner, "_gh") as gh:
        assert scanner.poll(scanner.configure(), watermark) == ([], watermark)
    gh.assert_not_called()

    scanner = _scanner()
    scanner._pr_status_snapshot = {
        "schema_version": 2,
        "committed": {},
        "candidate": {"scope": "bad", "statuses": {}},
        "candidate_watermark": watermark,
        "bootstrap_pending": False,
    }
    with patch.object(scanner, "_gh") as gh:
        assert scanner.poll(scanner.configure(), watermark) == ([], watermark)
    gh.assert_not_called()

    scanner = _scanner()
    invalid_configs = [
        {**scanner.configure(), "watch_repos": "acme/widgets"},
        {**scanner.configure(), "watch_repos": ["acme/widgets", "acme/widgets"]},
        {**scanner.configure(), "username": ["alice"]},
        {**scanner.configure(), "watch_ci": 1},
        {**scanner.configure(), "token_env": "PATH"},
    ]
    with patch.object(scanner, "_gh") as gh:
        for config in invalid_configs:
            assert scanner.poll(config, watermark) == ([], watermark)
        assert scanner.poll(scanner.configure(), "not-a-time") == ([], "not-a-time")
    gh.assert_not_called()


def test_malformed_late_notification_fails_the_whole_provider_page():
    scanner = _scanner()
    malformed = json.loads(json.dumps(NOTIFICATIONS[1]))
    malformed["subject"]["title"] = {"bad": "shape"}
    with patch.object(
        scanner,
        "_gh",
        return_value=json.dumps([NOTIFICATIONS[0], malformed]),
    ):
        with pytest.raises(RuntimeError, match="subject"):
            scanner._poll_notifications(
                scanner.configure(), "2026-07-15T09:00:00Z", False
            )


def test_graphql_missing_shape_and_repeated_cursor_fail_transactionally():
    scanner = _scanner()
    with patch.object(scanner, "_gh", return_value=json.dumps({"data": {}})):
        with pytest.raises(RuntimeError, match="pull request data"):
            scanner._poll_ci_status(
                "alice", [], {}, False, scanner.configure()
            )

    page = {
        "data": {
            "viewer": {
                "login": "alice",
                "pullRequests": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": "repeat"},
                },
            }
        }
    }
    with patch.object(scanner, "_gh", side_effect=[json.dumps(page), json.dumps(page)]):
        with pytest.raises(RuntimeError, match="endCursor"):
            scanner._poll_ci_status(
                "alice", [], {}, False, {**scanner.configure(), "max_pr_pages": 2}
            )


def test_state_commit_compares_timestamp_instants_not_text():
    scanner = _scanner()
    snapshot = {
        "schema_version": 3,
        "committed": {
            "boundary": "2026-07-15T08:00:00Z",
            "bootstrapped": True,
        },
        "candidate": {
            "boundary": "2026-07-15T03:00:00-07:00",
            "bootstrapped": True,
        },
        "candidate_watermark": "2026-07-15T03:00:00-07:00",
    }
    state = scanner._prepare_notification_state(
        snapshot, "2026-07-15T10:00:00Z", True
    )
    assert state["boundary"] == "2026-07-15T03:00:00-07:00"


def test_notification_urls_are_same_repository_and_exact_pr_paths():
    scanner = _scanner()
    assert scanner._api_url_to_web(
        "https://api.github.com/repos/acme/other/pulls/1", "acme/widgets"
    ) == ""
    assert GitHubScanner._pr_number_from_api_url(
        "https://api.github.com/repos/acme/widgets/pulls/1?redirect=evil"
    ) is None


def test_gh_environment_uses_only_selected_github_credentials():
    scanner = _scanner()
    scanner._token_env = "SAFE_TOKEN"
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        kwargs["stdout"].write(b"[]")
        return subprocess.CompletedProcess(args[0], 0)

    hostile = {
        "PATH": os.environ.get("PATH", ""),
        "SAFE_TOKEN": "selected-token",
        "GH_TOKEN": "attacker-token",
        "GH_HOST": "attacker.example",
        "GITHUB_ENTERPRISE_TOKEN": "attacker-enterprise-token",
    }
    with (
        patch.dict(os.environ, hostile, clear=True),
        patch.object(_mod.subprocess, "run", side_effect=fake_run),
    ):
        assert scanner._gh(["api", "/notifications"]) == "[]"

    assert captured["GH_TOKEN"] == "selected-token"
    assert captured["GH_HOST"] == "github.com"
    assert "SAFE_TOKEN" not in captured
    assert "GITHUB_ENTERPRISE_TOKEN" not in captured
