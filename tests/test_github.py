"""Tests for GitHub scanner — focus on pr_number plumbing for check_acted."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))

from sources import github as _github_mod  # noqa: E402

# Patch only the names bound into the github module, so the real snapshot_store
# stays untouched for other tests.
_github_mod.load_snapshot = MagicMock(side_effect=lambda *a, **kw: {})
_github_mod.save_snapshot = MagicMock()

GitHubScanner = _github_mod.GitHubScanner


SAMPLE_NOTIFICATIONS = [
    {
        "id": "notif-pr-review",
        "updated_at": "2026-04-13T10:00:00Z",
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
        "updated_at": "2026-04-13T10:05:00Z",
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
        "updated_at": "2026-04-13T10:10:00Z",
        "reason": "mention",
        "subject": {
            "title": "Bug in parser",
            "type": "Issue",
            "url": "https://api.github.com/repos/acme/widgets/issues/789",
        },
        "repository": {"full_name": "acme/widgets"},
    },
]


def _make_scanner():
    s = GitHubScanner()
    s._bootstrapped = True
    s._cli_available = True
    return s


class TestPrNumberHelper:
    def test_valid_pr_url(self):
        url = "https://api.github.com/repos/acme/widgets/pulls/123"
        assert GitHubScanner._pr_number_from_api_url(url) == 123

    def test_issue_url_returns_none(self):
        url = "https://api.github.com/repos/acme/widgets/issues/789"
        assert GitHubScanner._pr_number_from_api_url(url) is None

    def test_empty_url_returns_none(self):
        assert GitHubScanner._pr_number_from_api_url("") is None

    def test_malformed_url_returns_none(self):
        url = "https://api.github.com/repos/acme/widgets/pulls/not-a-number"
        assert GitHubScanner._pr_number_from_api_url(url) is None


class TestPollNotifications:
    def test_pr_notification_has_pr_number(self):
        scanner = _make_scanner()
        with patch.object(scanner, "_gh", return_value=json.dumps(SAMPLE_NOTIFICATIONS)):
            items = scanner._poll_notifications({}, "2026-04-13T09:00:00Z")

        review_pollen = [i for i in items if i["type"] == "review_needed"]
        assert len(review_pollen) == 1
        assert review_pollen[0]["metadata"]["pr_number"] == 123
        assert review_pollen[0]["metadata"]["repo"] == "acme/widgets"

    def test_pr_mention_has_pr_number(self):
        scanner = _make_scanner()
        with patch.object(scanner, "_gh", return_value=json.dumps(SAMPLE_NOTIFICATIONS)):
            items = scanner._poll_notifications({}, "2026-04-13T09:00:00Z")

        pr_mention = [
            i for i in items
            if i["type"] == "mention" and i["metadata"]["subject_type"] == "PullRequest"
        ]
        assert len(pr_mention) == 1
        assert pr_mention[0]["metadata"]["pr_number"] == 456

    def test_issue_notification_omits_pr_number(self):
        scanner = _make_scanner()
        with patch.object(scanner, "_gh", return_value=json.dumps(SAMPLE_NOTIFICATIONS)):
            items = scanner._poll_notifications({}, "2026-04-13T09:00:00Z")

        issue_items = [
            i for i in items if i["metadata"]["subject_type"] == "Issue"
        ]
        assert len(issue_items) == 1
        assert "pr_number" not in issue_items[0]["metadata"]

    def test_bootstrap_silences_first_poll(self):
        scanner = GitHubScanner()
        scanner._bootstrapped = False
        scanner._snapshot = {}
        scanner._cli_available = True
        with patch.object(scanner, "_gh", return_value=json.dumps(SAMPLE_NOTIFICATIONS)):
            items = scanner._poll_notifications({}, "2026-04-13T09:00:00Z")
        assert items == []
        assert "notif-pr-review" in scanner._snapshot


class TestCheckActedRoundTrip:
    def test_user_reviewed_returns_true(self):
        """End-to-end: poll produces pollen with pr_number, check_acted returns True when reviewed."""
        scanner = _make_scanner()
        with patch.object(scanner, "_gh", return_value=json.dumps(SAMPLE_NOTIFICATIONS)):
            items = scanner._poll_notifications({}, "2026-04-13T09:00:00Z")

        review_pollen = [i for i in items if i["type"] == "review_needed"][0]

        with patch.object(scanner, "_gh", return_value="1"):
            acted = scanner.check_acted(review_pollen, {"_username": "alice"})

        assert acted is True

    def test_user_not_reviewed_returns_false(self):
        scanner = _make_scanner()
        with patch.object(scanner, "_gh", return_value=json.dumps(SAMPLE_NOTIFICATIONS)):
            items = scanner._poll_notifications({}, "2026-04-13T09:00:00Z")

        review_pollen = [i for i in items if i["type"] == "review_needed"][0]

        with patch.object(scanner, "_gh", return_value="0"):
            acted = scanner.check_acted(review_pollen, {"_username": "alice"})

        assert acted is False

    def test_missing_pr_number_returns_false(self):
        """Safety: if pr_number somehow missing, check_acted short-circuits."""
        scanner = _make_scanner()
        pollen = {
            "type": "review_needed",
            "metadata": {"repo": "acme/widgets"},  # no pr_number
        }
        with patch.object(scanner, "_gh", return_value="1") as gh_mock:
            acted = scanner.check_acted(pollen, {"_username": "alice"})
        assert acted is False
        gh_mock.assert_not_called()
