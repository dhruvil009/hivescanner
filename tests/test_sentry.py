"""Functional tests for Sentry issue-set and spike transitions."""

import hashlib
import importlib.util
import json
import os
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "sentry_adapter",
    os.path.join(os.path.dirname(__file__), "..", "community", "sentry", "adapter.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SentryScanner = _mod.SentryScanner


def _issue(issue_id="12345", count=50, last_seen="2026-07-15T10:00:00Z"):
    return {
        "id": issue_id, "title": "ValueError: invalid literal", "level": "error",
        "platform": "python", "count": str(count), "lastSeen": last_seen,
        "permalink": f"https://sentry.io/issues/{issue_id}/", "shortId": "PROJ-1A",
    }


def _config(scanner, **changes):
    return {**scanner.configure(), "organization": "my-org", **changes}


def _scope(config):
    query = config["query"]
    if config["project"]:
        query = f"({query}) project:{config['project']}"
    return hashlib.sha256(json.dumps({
        "organization": config["organization"], "project": config["project"], "query": query,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _stored(count=50, alert=50, last_seen="2026-07-15T10:00:00Z"):
    return {"count": count, "alert_count": alert, "last_seen": last_seen}


def _state(config, issues=None, cursor=""):
    return json.dumps({
        "version": 3, "initialized": True, "scope": _scope(config),
        "issues": issues or {}, "detail_cursor": cursor,
    }, sort_keys=True, separators=(",", ":"))


def test_defaults_bound_issue_set_and_spike_thresholds():
    config = SentryScanner().configure()
    assert config["token_env"] == "SENTRY_TOKEN"
    assert config["query"] == "is:unresolved"
    assert config["min_event_delta"] == 10
    assert config["spike_ratio"] == 2.0
    assert config["max_items"] == 100
    assert SentryScanner.MAX_TRACKED_ISSUES == 500


def test_missing_credentials_and_invalid_slug_preserve_watermark(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.delenv("SENTRY_TOKEN", raising=False)
    assert scanner.poll(_config(scanner), "safe") == ([], "safe")
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    assert scanner.poll(_config(scanner, organization="../org"), "safe") == ([], "safe")
    assert scanner.poll({"token_env": "BAD-NAME", "organization": "org"}, "safe") == ([], "safe")


def test_first_scope_poll_is_quiet_and_snapshots_counts(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner)
    with patch.object(scanner, "_api", return_value=([_issue()], "")):
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    assert json.loads(watermark)["issues"]["12345"] == _stored()


def test_new_issue_after_bootstrap_emits_once(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner)
    with patch.object(scanner, "_api", return_value=([_issue()], "")):
        pollen, state = scanner.poll(config, _state(config))
    assert [item["type"] for item in pollen] == ["sentry_issue"]
    assert pollen[0]["id"].startswith("sentry-12345-")
    with patch.object(scanner, "_api", return_value=([_issue()], "")):
        pollen, _ = scanner.poll(config, state)
    assert pollen == []


def test_spike_requires_both_absolute_delta_and_ratio(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner, min_event_delta=10, spike_ratio=2.0)
    previous = _state(config, {"12345": _stored(count=10, alert=10)})
    with patch.object(scanner, "_api", return_value=([_issue(count=25)], "")):
        pollen, state = scanner.poll(config, previous)
    assert [item["type"] for item in pollen] == ["sentry_spike"]
    assert pollen[0]["metadata"]["count"] == 25

    # alert_count was advanced to 25; a small follow-up wave is quiet.
    with patch.object(scanner, "_api", return_value=([_issue(count=30)], "")):
        pollen, _ = scanner.poll(config, state)
    assert pollen == []


def test_count_increase_below_ratio_is_not_a_spike(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner, min_event_delta=10, spike_ratio=2.0)
    with patch.object(scanner, "_api", return_value=([_issue(count=120)], "")):
        pollen, _ = scanner.poll(config, _state(config, {"12345": _stored(count=100, alert=100)}))
    assert pollen == []


def test_link_cursor_pagination_is_followed(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner)
    responses = [([_issue("1")], "next"), ([_issue("2")], "")]
    with patch.object(scanner, "_api", side_effect=responses) as api:
        pollen, _ = scanner.poll(config, _state(config))
    assert len(pollen) == 2
    assert api.call_args_list[1].args[2]["cursor"] == "next"


def test_resolved_missing_issue_is_confirmed_by_detail(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner)
    with (
        patch.object(scanner, "_api", return_value=([], "")),
        patch.object(scanner, "_api_object", return_value={
            "status": "resolved", "title": "Resolved issue", "lastSeen": "2026-07-15T11:00:00Z",
        }),
    ):
        pollen, watermark = scanner.poll(config, _state(config, {"12345": _stored()}))
    assert [item["type"] for item in pollen] == ["sentry_resolved"]
    assert json.loads(watermark)["issues"] == {}


def test_unresolved_detail_during_index_lag_is_retained(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner)
    with (
        patch.object(scanner, "_api", return_value=([], "")),
        patch.object(scanner, "_api_object", return_value={"status": "unresolved"}),
    ):
        pollen, watermark = scanner.poll(config, _state(config, {"12345": _stored()}))
    assert pollen == []
    assert "12345" in json.loads(watermark)["issues"]


def test_detail_cursor_rotates_past_persistent_failures(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner)
    issues = {f"I{i:02d}": _stored() for i in range(12)}
    called = []

    def detail(path, token):
        called.append(path.rsplit("/", 2)[-2])
        return None

    with (
        patch.object(scanner, "_api", return_value=([], "")),
        patch.object(scanner, "_api_object", side_effect=detail),
    ):
        _, watermark = scanner.poll(config, _state(config, issues))
        scanner.poll(config, watermark)
    assert called[:10] == [f"I{i:02d}" for i in range(10)]
    assert called[10:12] == ["I10", "I11"]


def test_project_filter_is_always_added_as_outer_constraint(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner, project="web", query="is:unresolved notproject:foo")
    with patch.object(scanner, "_api", return_value=([], "")) as api:
        scanner.poll(config, _state(config))
    assert api.call_args.args[2]["query"] == (
        "(is:unresolved notproject:foo) project:web"
    )

    conflicting = _config(
        scanner, project="web", query="is:unresolved project:other OR level:fatal"
    )
    with patch.object(scanner, "_api", return_value=([], "")) as api:
        scanner.poll(conflicting, _state(conflicting))
    assert api.call_args.args[2]["query"] == (
        "(is:unresolved project:other OR level:fatal) project:web"
    )


def test_api_error_page_overflow_and_issue_overflow_fail_closed(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner)
    state = _state(config)
    with patch.object(scanner, "_api", return_value=(None, "")):
        assert scanner.poll(config, state) == ([], state)
    with patch.object(scanner, "_api", return_value=([], "cursor")):
        assert scanner.poll(_config(scanner, max_pages=1), state) == ([], state)
    with patch.object(scanner, "_api", return_value=([_issue(str(i)) for i in range(501)], "")):
        assert scanner.poll(config, state) == ([], state)


def test_malformed_issue_page_is_transactional(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner)
    state = _state(config)
    for issues in (
        [_issue("good"), {**_issue("bad"), "count": "not-a-number"}],
        [_issue("same"), _issue("same")],
        [{**_issue(), "title": {"bad": "shape"}}],
    ):
        with patch.object(scanner, "_api", return_value=(issues, "")):
            assert scanner.poll(config, state) == ([], state)


def test_bad_state_and_pagination_cursor_preserve_watermark(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner)
    corrupt = json.loads(_state(config, {"12345": _stored()}))
    corrupt["issues"]["12345"]["count"] = "50"
    corrupt_state = json.dumps(corrupt)
    with patch.object(scanner, "_api") as api:
        assert scanner.poll(config, corrupt_state) == ([], corrupt_state)
    api.assert_not_called()

    state = _state(config)
    with patch.object(scanner, "_api", return_value=([], {"bad": "cursor"})):
        assert scanner.poll(config, state) == ([], state)
    with patch.object(scanner, "_api", return_value=([], "repeat")):
        assert scanner.poll(config, state) == ([], state)


def test_malformed_terminal_detail_is_retained(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    config = _config(scanner)
    with (
        patch.object(scanner, "_api", return_value=([], "")),
        patch.object(
            scanner,
            "_api_object",
            return_value={"status": "resolved", "count": {"bad": "shape"}},
        ),
    ):
        pollen, watermark = scanner.poll(
            config, _state(config, {"12345": _stored()})
        )
    assert pollen == []
    assert "12345" in json.loads(watermark)["issues"]


def test_config_and_json_types_are_strict(monkeypatch):
    scanner = SentryScanner()
    monkeypatch.setenv("SENTRY_TOKEN", "token")
    for config in (
        _config(scanner, organization={"slug": "my-org"}),
        _config(scanner, project=["web"]),
        _config(scanner, query={"query": "is:unresolved"}),
        _config(scanner, token_env=123),
    ):
        with patch.object(scanner, "_api") as api:
            assert scanner.poll(config, "safe") == ([], "safe")
        api.assert_not_called()
    try:
        _mod._strict_json('[{"id":"1"},{"id":"1","id":"2"}]')
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate JSON keys must be rejected")
