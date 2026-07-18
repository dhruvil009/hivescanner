"""Functional tests for the Gmail scanner's real list/get contract."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch


_EMAIL_PATH = os.path.join(
    os.path.dirname(__file__), "..", "workers", "sources", "email.py"
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
_spec = importlib.util.spec_from_file_location("email_scanner", _EMAIL_PATH)
_email_mod = importlib.util.module_from_spec(_spec)
sys.modules["email_scanner"] = _email_mod
_spec.loader.exec_module(_email_mod)
EmailScanner = _email_mod.EmailScanner
WATERMARK = "2026-07-15T00:00:00Z"


def _scanner(*, ready=False):
    with (
        patch.object(_email_mod, "load_snapshot", return_value={}),
        patch.object(_email_mod, "snapshot_exists", return_value=ready),
    ):
        scanner = EmailScanner()
    scanner._cli_available = True
    if ready:
        scope = hashlib.sha256(b"in:inbox").hexdigest()[:16]
        scanner._snapshot = {
            "schema_version": 3,
            "committed": {
                "scope": scope,
                "boundary_ms": 1_786_000_000_000,
                "seen_ids": [],
            },
            "candidate": {},
            "candidate_watermark": "",
            "bootstrap_pending": False,
        }
    return scanner


def _message(msg_id, sender="Alice <alice@example.com>", subject="Project update"):
    return {
        "id": msg_id,
        "internalDate": "1786000001000",
        "snippet": "Here is the latest project update.",
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Wed, 15 Jul 2026 10:00:00 -0700"},
            ]
        },
    }


def _api(stub_ids, messages, *, list_pages=None):
    pages = iter(list_pages) if list_pages is not None else None

    def fake(args, timeout=15):
        if "list" in args:
            if pages is not None:
                return json.dumps(next(pages))
            return json.dumps({"messages": [{"id": value} for value in stub_ids]})
        params = json.loads(args[args.index("--params") + 1])
        return json.dumps(messages[params["id"]])

    return fake


REQUIRED_KEYS = {
    "id", "source", "type", "title", "preview", "discovered_at",
    "author", "author_name", "group", "url", "metadata",
}


def test_configure_matches_bounded_gmail_defaults():
    config = _scanner().configure()
    assert config == {
        "enabled": False,
        "vip_senders": [],
        "query": "in:inbox",
        "max_emails": 20,
        "max_pages": 5,
        "overlap_seconds": 300,
    }


def test_missing_cli_preserves_watermark():
    scanner = _scanner()
    scanner._cli_available = False
    assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)


def test_first_install_queries_only_after_start_and_stays_quiet():
    scanner = _scanner()
    calls = []

    def fake(args, timeout=15):
        calls.append(args)
        return json.dumps({"messages": []})

    with patch.object(scanner, "_gws", side_effect=fake), patch.object(_email_mod, "save_snapshot"):
        pollen, watermark = scanner.poll(scanner.configure(), "")

    assert pollen == []
    params = json.loads(calls[0][calls[0].index("--params") + 1])
    assert params["q"].startswith("(in:inbox) after:")
    assert scanner._snapshot["bootstrap_pending"] is True
    assert scanner._snapshot["candidate_watermark"] == watermark


def test_real_list_then_metadata_get_emits_new_mail():
    scanner = _scanner(ready=True)
    with (
        patch.object(scanner, "_gws", side_effect=_api(["msg001"], {"msg001": _message("msg001")})),
        patch.object(_email_mod, "save_snapshot"),
    ):
        pollen, _ = scanner.poll(scanner.configure(), "2026-07-15T00:00:00Z")

    assert len(pollen) == 1
    item = pollen[0]
    assert item["id"] == "email-msg001"
    assert item["type"] == "email_new"
    assert item["author"] == "Alice <alice@example.com>"
    assert item["metadata"]["internal_date_ms"] == 1_786_000_001_000
    assert REQUIRED_KEYS <= item.keys()


def test_vip_matching_uses_exact_parsed_address_not_substrings():
    scanner = _scanner(ready=True)
    messages = {
        "vip": _message("vip", "CEO <CEO@company.com>", "Board meeting"),
        "lookalike": _message("lookalike", "attacker+ceo@company.com.evil.test", "Noise"),
    }
    config = {**scanner.configure(), "vip_senders": ["ceo@company.com"]}
    with (
        patch.object(scanner, "_gws", side_effect=_api(["lookalike", "vip"], messages)),
        patch.object(_email_mod, "save_snapshot"),
    ):
        pollen, _ = scanner.poll(config, "2026-07-15T00:00:00Z")

    by_id = {item["id"]: item for item in pollen}
    assert by_id["email-vip"]["type"] == "email_urgent"
    assert by_id["email-lookalike"]["type"] == "email_new"


def test_newest_first_list_is_drained_oldest_first_without_advancing_boundary():
    scanner = _scanner(ready=True)
    config = {**scanner.configure(), "max_emails": 2}
    ids = ["newest", "middle", "oldest"]
    messages = {value: _message(value) for value in ids}
    with (
        patch.object(scanner, "_gws", side_effect=_api(ids, messages)),
        patch.object(_email_mod, "save_snapshot"),
    ):
        pollen, _ = scanner.poll(config, "2026-07-15T00:00:00Z")

    assert [item["id"] for item in pollen] == ["email-oldest", "email-middle"]
    assert scanner._snapshot["candidate"]["boundary_ms"] == 1_786_000_000_000
    assert scanner._snapshot["candidate"]["seen_ids"][-2:] == ["oldest", "middle"]


def test_committed_seen_ids_are_not_reemitted_during_overlap():
    scanner = _scanner(ready=True)
    scanner._snapshot["committed"]["seen_ids"] = ["already"]
    messages = {"already": _message("already"), "new": _message("new")}
    with (
        patch.object(scanner, "_gws", side_effect=_api(["new", "already"], messages)),
        patch.object(_email_mod, "save_snapshot"),
    ):
        pollen, _ = scanner.poll(scanner.configure(), "2026-07-15T00:00:00Z")
    assert [item["id"] for item in pollen] == ["email-new"]


def test_pagination_exhaustion_fails_closed_without_watermark_advance():
    scanner = _scanner(ready=True)
    config = {**scanner.configure(), "max_pages": 1}
    pages = [{"messages": [{"id": "a"}], "nextPageToken": "more"}]
    with patch.object(scanner, "_gws", side_effect=_api([], {}, list_pages=pages)):
        assert scanner.poll(config, WATERMARK) == ([], WATERMARK)


def test_invalid_query_or_list_shape_fails_closed():
    scanner = _scanner(ready=True)
    assert scanner.poll({**scanner.configure(), "query": "x" * 5001}, WATERMARK) == ([], WATERMARK)
    with patch.object(scanner, "_gws", return_value=json.dumps([])):
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)


def test_detail_failure_commits_successful_ids_and_holds_boundary():
    scanner = _scanner(ready=True)
    original_boundary = scanner._snapshot["committed"]["boundary_ms"]

    def fake(args, timeout=15):
        if "list" in args:
            return json.dumps({"messages": [{"id": "bad"}, {"id": "good"}]})
        params = json.loads(args[args.index("--params") + 1])
        return json.dumps(_message("good")) if params["id"] == "good" else None

    with (
        patch.object(scanner, "_gws", side_effect=fake),
        patch.object(_email_mod, "save_snapshot"),
    ):
        pollen, watermark = scanner.poll(
            scanner.configure(), "2026-07-15T00:00:00Z"
        )

    assert [item["id"] for item in pollen] == ["email-good"]
    assert watermark != "2026-07-15T00:00:00Z"
    assert scanner._snapshot["candidate"]["seen_ids"] == ["good"]
    assert scanner._snapshot["candidate"]["boundary_ms"] == original_boundary


def test_malformed_list_stub_preserves_state_without_detail_calls():
    scanner = _scanner(ready=True)
    with patch.object(
        scanner,
        "_gws",
        return_value=json.dumps({"messages": [{"threadId": "missing-id"}]}),
    ) as gws:
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)
    assert gws.call_count == 1


def test_config_types_are_not_silently_coerced():
    scanner = _scanner(ready=True)
    for config in (
        {**scanner.configure(), "query": {"unexpected": "object"}},
        {**scanner.configure(), "query": " in:inbox"},
        {**scanner.configure(), "vip_senders": "ceo@example.com"},
        {**scanner.configure(), "max_pages": "5"},
    ):
        with patch.object(scanner, "_gws") as gws:
            assert scanner.poll(config, WATERMARK) == ([], WATERMARK)
        gws.assert_not_called()


def test_duplicate_json_and_malformed_detail_preserve_boundary():
    scanner = _scanner(ready=True)
    with patch.object(scanner, "_gws", return_value='{"messages":[],"messages":[]}'):
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)

    original_boundary = scanner._snapshot["committed"]["boundary_ms"]
    malformed = _message("bad")
    malformed["snippet"] = {"not": "text"}
    with (
        patch.object(scanner, "_gws", side_effect=_api(["bad"], {"bad": malformed})),
        patch.object(_email_mod, "save_snapshot"),
    ):
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)
    assert scanner._snapshot["committed"]["boundary_ms"] == original_boundary


def test_invalid_current_snapshot_is_not_treated_as_a_fresh_bootstrap():
    scanner = _scanner(ready=True)
    scanner._snapshot["committed"]["seen_ids"] = [123]
    with patch.object(scanner, "_gws") as gws:
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)
    gws.assert_not_called()


def test_gws_environment_drops_response_mutating_overrides():
    scanner = _scanner()
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        kwargs["stdout"].write(b"{}")
        return subprocess.CompletedProcess(args[0], 0)

    hostile = {
        "GOOGLE_WORKSPACE_CLI_SANITIZE_TEMPLATE": "projects/attacker/templates/x",
        "GOOGLE_WORKSPACE_CLI_SANITIZE_MODE": "sanitize",
        "GOOGLE_WORKSPACE_CLI_LOG_FILE": "/tmp/leak",
        "GOOGLE_APPLICATION_CREDENTIALS": "/safe/auth.json",
    }
    with (
        patch.dict(os.environ, hostile, clear=True),
        patch.object(_email_mod.subprocess, "run", side_effect=fake_run),
    ):
        assert scanner._gws(["gmail", "users", "messages", "list"]) == "{}"

    assert captured["GOOGLE_APPLICATION_CREDENTIALS"] == "/safe/auth.json"
    assert not any("SANITIZE" in key or key.endswith("LOG_FILE") for key in captured)


def test_invalid_config_and_watermark_are_rejected_before_tool_install():
    scanner = _scanner(ready=True)
    scanner._cli_available = None
    invalid_configs = [
        {**scanner.configure(), "vip_senders": [""]},
        {
            **scanner.configure(),
            "vip_senders": ["CEO@example.com", "ceo@example.com"],
        },
        {**scanner.configure(), "max_emails": True},
    ]
    with patch.object(_email_mod, "ensure_tool") as ensure:
        for config in invalid_configs:
            assert scanner.poll(config, WATERMARK) == ([], WATERMARK)
        assert scanner.poll(scanner.configure(), "not-a-time") == ([], "not-a-time")
    ensure.assert_not_called()


def test_unknown_snapshot_and_malformed_staged_state_fail_before_gws():
    scanner = _scanner(ready=True)
    scanner._snapshot = {"schema_version": []}
    with patch.object(scanner, "_gws") as gws:
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)
    gws.assert_not_called()

    scanner = _scanner(ready=True)
    scanner._snapshot["candidate"] = {
        **scanner._snapshot["committed"],
        "seen_ids": [123],
    }
    scanner._snapshot["candidate_watermark"] = "2026-07-15T00:01:00Z"
    with patch.object(scanner, "_gws") as gws:
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)
    gws.assert_not_called()


def test_staged_state_uses_actual_timestamp_comparison():
    scanner = _scanner(ready=True)
    scanner._snapshot["candidate"] = {
        **scanner._snapshot["committed"],
        "seen_ids": ["staged"],
    }
    scanner._snapshot["candidate_watermark"] = "2026-07-15T01:00:00+01:00"
    with patch.object(
        scanner,
        "_gws",
        return_value=json.dumps({"messages": [{"id": "staged"}]}),
    ) as gws, patch.object(_email_mod, "save_snapshot"):
        pollen, _ = scanner.poll(
            scanner.configure(), "2026-07-15T00:30:00Z"
        )
    assert pollen == []
    assert gws.call_count == 1


def test_repeated_page_token_duplicate_ids_and_oversized_page_fail_closed():
    scanner = _scanner(ready=True)
    repeated = [
        {"messages": [{"id": "one"}], "nextPageToken": "same"},
        {"messages": [{"id": "two"}], "nextPageToken": "same"},
    ]
    with patch.object(scanner, "_gws", side_effect=_api([], {}, list_pages=repeated)):
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)

    duplicate = [{"messages": [{"id": "same"}, {"id": "same"}]}]
    with patch.object(scanner, "_gws", side_effect=_api([], {}, list_pages=duplicate)):
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)

    oversized = [{"messages": [{"id": str(index)} for index in range(501)]}]
    with patch.object(scanner, "_gws", side_effect=_api([], {}, list_pages=oversized)):
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)


def test_error_wrapper_and_duplicate_security_headers_fail_closed():
    scanner = _scanner(ready=True)
    with patch.object(scanner, "_gws", return_value='{"error":null}'):
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)

    message = _message("bad")
    message["payload"]["headers"].append(
        {"name": "from", "value": "attacker@example.test"}
    )
    with patch.object(
        scanner,
        "_gws",
        side_effect=_api(["bad"], {"bad": message}),
    ):
        assert scanner.poll(scanner.configure(), WATERMARK) == ([], WATERMARK)


def test_gws_output_must_be_utf8():
    scanner = _scanner()

    def invalid_utf8(*args, **kwargs):
        kwargs["stdout"].write(b"\xff")
        return SimpleNamespace(returncode=0)

    with patch.object(_email_mod.subprocess, "run", side_effect=invalid_utf8):
        assert scanner._gws(["gmail", "users", "messages", "list"]) is None
