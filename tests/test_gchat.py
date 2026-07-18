"""Functional tests for Google Chat's paginated gws API contract."""

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
_PATH = os.path.join(os.path.dirname(__file__), "..", "workers", "sources", "gchat.py")
_spec = importlib.util.spec_from_file_location("gchat", _PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["gchat"] = _mod
_spec.loader.exec_module(_mod)
GChatScanner = _mod.GChatScanner


def _scanner(*, ready=False):
    with (
        patch.object(_mod, "load_snapshot", return_value={}),
        patch.object(_mod, "snapshot_exists", return_value=ready),
    ):
        scanner = GChatScanner()
    scanner._cli_available = True
    if ready:
        scanner._snapshot = {
            "schema_version": 3,
            "committed": {},
            "candidate": {},
            "candidate_watermark": "",
            "bootstrap_pending": False,
            "dm_spaces": [],
            "dm_refreshed_at": "",
        }
    return scanner


def _message(
    msg_id="msg001",
    *,
    space="spaces/AAAA",
    text="Hey, can you review this?",
    sender="users/12345",
    annotations=None,
    fractional=False,
):
    timestamp = datetime.now(timezone.utc).isoformat(
        timespec="microseconds" if fractional else "seconds"
    ).replace("+00:00", "Z")
    return {
        "name": f"{space}/messages/{msg_id}",
        "sender": {"displayName": "Alice", "name": sender},
        "text": text,
        "createTime": timestamp,
        "annotations": annotations or [],
    }


def _config(scanner, **changes):
    return {
        **scanner.configure(),
        "watch_dms": False,
        **changes,
    }


REQUIRED_KEYS = {
    "id", "source", "type", "title", "preview", "discovered_at",
    "author", "author_name", "group", "url", "metadata",
}


def test_configure_exposes_explicit_and_discovered_dm_controls():
    config = _scanner().configure()
    assert config["enabled"] is False
    assert config["watch_spaces"] == []
    assert config["watch_dm_spaces"] == []
    assert config["watch_dms"] is True
    assert config["max_messages"] == 20
    assert config["max_pages"] == 10
    assert config["spaces_per_poll"] == 5


def test_missing_cli_preserves_watermark():
    scanner = _scanner()
    scanner._cli_available = False
    assert scanner.poll(scanner.configure(), "safe") == ([], "safe")


def test_explicit_dm_uses_messages_wrapper_and_space_qualified_id():
    scanner = _scanner(ready=True)
    message = _message()
    config = _config(scanner, watch_dm_spaces=["spaces/AAAA"])
    with (
        patch.object(scanner, "_gws", return_value=json.dumps({"messages": [message]})),
        patch.object(_mod, "save_snapshot"),
    ):
        pollen, _ = scanner.poll(config, "2026-07-15T00:00:00Z")

    assert len(pollen) == 1
    item = pollen[0]
    assert item["type"] == "gchat_dm"
    assert item["metadata"]["message_name"].endswith("/msg001")
    assert item["metadata"]["space_id"] == "spaces/AAAA"
    assert item["id"].startswith("gchat-") and item["id"].endswith("-msg001")
    assert REQUIRED_KEYS <= item.keys()


def test_user_mention_requires_the_configured_identity():
    scanner = _scanner(ready=True)
    annotations = [{
        "type": "USER_MENTION",
        "userMention": {"user": {"name": "users/me"}},
    }]
    message = _message(space="spaces/ROOM", annotations=annotations)
    config = _config(
        scanner,
        watch_spaces=["spaces/ROOM"],
        user_resource="users/me",
    )
    with (
        patch.object(scanner, "_gws", return_value=json.dumps({"messages": [message]})),
        patch.object(_mod, "save_snapshot"),
    ):
        pollen, _ = scanner.poll(config, "2026-07-15T00:00:00Z")
    assert [item["type"] for item in pollen] == ["gchat_mention"]

    wrong = _message(
        "msg002",
        space="spaces/ROOM",
        annotations=[{
            "type": "USER_MENTION",
            "userMention": {"user": {"name": "users/someone-else"}},
        }],
    )
    with (
        patch.object(scanner, "_gws", return_value=json.dumps({"messages": [wrong]})),
        patch.object(_mod, "save_snapshot"),
    ):
        pollen, _ = scanner.poll(config, "2026-07-15T00:01:00Z")
    assert pollen == []


def test_self_messages_and_unmentioned_room_chatter_are_suppressed():
    scanner = _scanner(ready=True)
    config = _config(
        scanner,
        watch_spaces=["spaces/ROOM"],
        user_resource="users/me",
        username="dhruvil",
    )
    messages = [
        _message("self", space="spaces/ROOM", sender="users/me", text="@dhruvil self"),
        _message("noise", space="spaces/ROOM", text="hello everyone"),
    ]
    with (
        patch.object(scanner, "_gws", return_value=json.dumps({"messages": messages})),
        patch.object(_mod, "save_snapshot"),
    ):
        pollen, _ = scanner.poll(config, "2026-07-15T00:00:00Z")
    assert pollen == []


def test_first_poll_is_quiet_and_requires_watermark_commit_to_finish_bootstrap():
    scanner = _scanner()
    config = _config(scanner, watch_dm_spaces=["spaces/AAAA"])
    message = _message()
    with (
        patch.object(scanner, "_gws", return_value=json.dumps({"messages": [message]})),
        patch.object(_mod, "save_snapshot"),
    ):
        pollen, watermark = scanner.poll(config, "")
    assert pollen == []
    assert scanner._snapshot["bootstrap_pending"] is True

    with (
        patch.object(scanner, "_gws", return_value=json.dumps({"messages": [message]})),
        patch.object(_mod, "save_snapshot"),
    ):
        pollen, _ = scanner.poll(config, watermark)
    assert pollen == []
    assert scanner._snapshot["bootstrap_pending"] is False


def test_dm_discovery_uses_space_list_and_caches_results():
    scanner = _scanner(ready=True)
    calls = []

    def fake(args, timeout=20):
        calls.append(args)
        if args[:3] == ["chat", "spaces", "list"]:
            return json.dumps({
                "spaces": [
                    {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"},
                    {"name": "spaces/ROOM", "spaceType": "SPACE"},
                ]
            })
        return json.dumps({"messages": []})

    with patch.object(scanner, "_gws", side_effect=fake), patch.object(_mod, "save_snapshot"):
        scanner.poll(scanner.configure(), "2026-07-15T00:00:00Z")
    assert scanner._snapshot["dm_spaces"] == ["spaces/DM1"]
    assert any(args[:3] == ["chat", "spaces", "list"] for args in calls)
    assert any("spaces/DM1" in " ".join(args) for args in calls)


def test_failure_in_one_space_commits_only_the_successful_space_boundary():
    scanner = _scanner(ready=True)
    config = _config(
        scanner,
        watch_dm_spaces=["spaces/AAAA", "spaces/BBBB"],
    )

    def fake(args, timeout=20):
        params = json.loads(args[args.index("--params") + 1])
        if params["parent"] == "spaces/AAAA":
            return None
        return json.dumps({"messages": [_message("from-b", space="spaces/BBBB")]})

    committed = "2026-07-15T00:00:00Z"
    with (
        patch.object(scanner, "_gws", side_effect=fake),
        patch.object(_mod, "save_snapshot"),
    ):
        pollen, watermark = scanner.poll(config, committed)
    assert [item["metadata"]["space_id"] for item in pollen] == ["spaces/BBBB"]
    assert watermark != committed
    assert scanner._snapshot["candidate_space_times"]["spaces/AAAA"] == committed
    assert scanner._snapshot["candidate_space_times"]["spaces/BBBB"] == watermark


def test_uncommitted_partial_poll_replays_the_same_space_and_message():
    scanner = _scanner(ready=True)
    config = _config(
        scanner,
        watch_dm_spaces=["spaces/AAAA", "spaces/BBBB"],
    )

    def fake(args, timeout=20):
        params = json.loads(args[args.index("--params") + 1])
        if params["parent"] == "spaces/AAAA":
            return None
        return json.dumps({"messages": [_message("from-b", space="spaces/BBBB")]})

    committed = "2026-07-15T00:00:00Z"
    with (
        patch.object(scanner, "_gws", side_effect=fake),
        patch.object(_mod, "save_snapshot"),
    ):
        first, _ = scanner.poll(config, committed)
        replay, _ = scanner.poll(config, committed)
    assert [item["id"] for item in replay] == [item["id"] for item in first]


def test_space_rotation_bounds_each_poll_and_advances_transactionally():
    scanner = _scanner(ready=True)
    config = _config(
        scanner,
        watch_dm_spaces=[f"spaces/S{index}" for index in range(6)],
        spaces_per_poll=2,
    )
    parents = []

    def fake(args, timeout=20):
        params = json.loads(args[args.index("--params") + 1])
        parents.append(params["parent"])
        return json.dumps({"messages": []})

    with (
        patch.object(scanner, "_gws", side_effect=fake),
        patch.object(_mod, "save_snapshot"),
    ):
        _, first_watermark = scanner.poll(config, "2026-07-15T00:00:00Z")
        assert parents == ["spaces/S0", "spaces/S1"]
        parents.clear()
        scanner.poll(config, first_watermark)
    assert parents == ["spaces/S2", "spaces/S3"]


def test_failed_rotation_slice_does_not_starve_unselected_spaces():
    scanner = _scanner(ready=True)
    config = _config(
        scanner,
        watch_dm_spaces=[f"spaces/S{index}" for index in range(6)],
        spaces_per_poll=2,
    )
    parents = []

    def fake(args, timeout=20):
        params = json.loads(args[args.index("--params") + 1])
        parents.append(params["parent"])
        return None if params["parent"] in {"spaces/S0", "spaces/S1"} else json.dumps({"messages": []})

    committed = "2026-07-15T00:00:00Z"
    with (
        patch.object(scanner, "_gws", side_effect=fake),
        patch.object(_mod, "save_snapshot"),
    ):
        _, first_watermark = scanner.poll(config, committed)
        assert first_watermark != committed
        parents.clear()
        scanner.poll(config, first_watermark)
    assert parents == ["spaces/S2", "spaces/S3"]


def test_fractional_rfc3339_timestamp_survives_overlap_retention():
    scanner = _scanner(ready=True)
    config = _config(scanner, watch_dm_spaces=["spaces/AAAA"])
    message = _message(fractional=True)
    with (
        patch.object(scanner, "_gws", return_value=json.dumps({"messages": [message]})),
        patch.object(_mod, "save_snapshot"),
    ):
        scanner.poll(config, "2026-07-15T00:00:00Z")
    assert message["name"] in scanner._snapshot["candidate"]


def test_invalid_space_and_pagination_exhaustion_fail_closed():
    scanner = _scanner(ready=True)
    invalid = _config(scanner, watch_spaces=["spaces/../../escape"])
    assert scanner.poll(invalid, "safe") == ([], "safe")

    config = _config(scanner, watch_spaces=["spaces/ROOM"], max_pages=1)
    watermark = "2026-07-15T00:00:00Z"
    with patch.object(
        scanner,
        "_gws",
        return_value=json.dumps({"messages": [], "nextPageToken": "more"}),
    ), patch.object(_mod, "save_snapshot"):
        assert scanner.poll(config, watermark) == ([], watermark)


def test_malformed_message_or_page_token_preserves_the_space_boundary():
    scanner = _scanner(ready=True)
    config = _config(scanner, watch_spaces=["spaces/ROOM"])
    watermark = "2026-07-15T00:00:00Z"
    malformed = {
        "name": "spaces/OTHER/messages/wrong-parent",
        "createTime": "2026-07-15T00:01:00Z",
    }
    with (
        patch.object(scanner, "_gws", return_value=json.dumps({"messages": [malformed]})),
        patch.object(_mod, "save_snapshot"),
    ):
        assert scanner.poll(config, watermark) == ([], watermark)
    with (
        patch.object(
            scanner,
            "_gws",
            return_value=json.dumps({"messages": [], "nextPageToken": 7}),
        ),
        patch.object(_mod, "save_snapshot"),
    ):
        assert scanner.poll(config, watermark) == ([], watermark)


def test_invalid_current_snapshot_config_and_watermark_fail_before_provider_call():
    watermark = "2026-07-15T00:00:00Z"
    invalid_snapshots = []
    for key, value in [
        ("bootstrap_pending", "false"),
        ("candidate_watermark", 123),
        ("dm_spaces", ["../../escape"]),
        ("schema_version", 4),
    ]:
        scanner = _scanner(ready=True)
        scanner._snapshot[key] = value
        invalid_snapshots.append(scanner)
    for scanner in invalid_snapshots:
        with patch.object(scanner, "_gws") as gws:
            assert scanner.poll(
                _config(scanner, watch_dm_spaces=["spaces/AAAA"]), watermark
            ) == ([], watermark)
        gws.assert_not_called()

    scanner = _scanner(ready=True)
    invalid_configs = [
        _config(scanner, watch_spaces="spaces/AAAA"),
        _config(scanner, watch_spaces=["AAAA", "spaces/AAAA"]),
        _config(
            scanner,
            watch_spaces=["spaces/AAAA"],
            watch_dm_spaces=["AAAA"],
        ),
        _config(scanner, username="@@me"),
        _config(scanner, user_resource=["users/me"]),
    ]
    with patch.object(scanner, "_gws") as gws:
        for config in invalid_configs:
            assert scanner.poll(config, watermark) == ([], watermark)
        assert scanner.poll(scanner.configure(), "not-a-timestamp") == (
            [],
            "not-a-timestamp",
        )
    gws.assert_not_called()


def test_malformed_late_message_and_wrong_order_are_transactional():
    scanner = _scanner(ready=True)
    config = _config(scanner, watch_dm_spaces=["spaces/AAAA"])
    watermark = "2026-07-15T00:00:00Z"
    newest = _message("newest")
    newest["createTime"] = "2026-07-15T00:02:00Z"
    malformed = _message("bad")
    malformed["createTime"] = "2026-07-15T00:01:00Z"
    malformed["annotations"] = [{
        "type": "USER_MENTION",
        "userMention": {"user": {"name": {"bad": "shape"}}},
    }]
    oldest = _message("oldest")
    oldest["createTime"] = "2026-07-15T00:01:00Z"
    for messages in ([newest, malformed], [oldest, newest]):
        with (
            patch.object(
                scanner,
                "_gws",
                return_value=json.dumps({"messages": messages}),
            ),
            patch.object(_mod, "save_snapshot"),
        ):
            assert scanner.poll(config, watermark) == ([], watermark)


def test_dm_discovery_repeated_token_is_not_cached():
    scanner = _scanner(ready=True)
    responses = [
        json.dumps({"spaces": [], "nextPageToken": "repeat"}),
        json.dumps({"spaces": [], "nextPageToken": "repeat"}),
    ]
    with (
        patch.object(scanner, "_gws", side_effect=responses) as gws,
        patch.object(_mod, "save_snapshot") as save,
    ):
        assert scanner.poll(
            scanner.configure(), "2026-07-15T00:00:00Z"
        ) == ([], "2026-07-15T00:00:00Z")
    assert gws.call_count == 2
    save.assert_not_called()


def test_provider_json_rejects_duplicate_keys_and_nonfinite_numbers():
    assert GChatScanner._parse_page(
        '{"messages":[],"messages":[]}', "messages"
    ) is None
    assert GChatScanner._parse_page(
        '{"messages":[],"value":NaN}', "messages"
    ) is None


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
        patch.object(_mod.subprocess, "run", side_effect=fake_run),
    ):
        assert scanner._gws(["chat", "spaces", "list"]) == "{}"

    assert captured["GOOGLE_APPLICATION_CREDENTIALS"] == "/safe/auth.json"
    assert not any("SANITIZE" in key or key.endswith("LOG_FILE") for key in captured)
