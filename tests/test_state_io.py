"""Adversarial tests for durable JSON state files."""

import json
import os
import stat
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
from state_io import StateFileError, advisory_lock, atomic_write_json, load_json


def test_missing_file_returns_default(tmp_path):
    default = {"watermark": "initial"}
    assert load_json(tmp_path / "missing.json", default, dict) is default


def test_atomic_round_trip_uses_private_permissions(tmp_path):
    path = tmp_path / "private" / "state.json"
    atomic_write_json(path, {"watermark": "200", "items": [1, 2]})
    assert load_json(path, {}, dict) == {"watermark": "200", "items": [1, 2]}
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "raw",
    [
        b'{"watermark":',
        b'{"same": 1, "same": 2}',
        b'{"value": NaN}',
        b'{"value": Infinity}',
        b'\xff\xfe',
    ],
)
def test_corrupt_or_ambiguous_json_raises_without_rewriting(tmp_path, raw):
    path = tmp_path / "state.json"
    path.write_bytes(raw)
    with pytest.raises(StateFileError, match="left untouched"):
        load_json(path, {}, dict)
    assert path.read_bytes() == raw


def test_wrong_top_level_type_is_rejected(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[]")
    with pytest.raises(StateFileError, match="expected top-level dict"):
        load_json(path, {}, dict)


def test_oversized_file_is_rejected_before_parse(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b" " * 17)
    with pytest.raises(StateFileError, match="limit 16"):
        load_json(path, {}, dict, max_bytes=16)


def test_symlink_state_is_never_followed(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"secret": "must-not-load"}))
    link = tmp_path / "state.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    with pytest.raises(StateFileError, match="symlink"):
        load_json(link, {}, dict)


def test_non_regular_state_path_is_rejected(tmp_path):
    directory = tmp_path / "state.json"
    directory.mkdir()
    with pytest.raises(StateFileError, match="regular file"):
        load_json(directory, {}, dict)


def test_serialization_failure_preserves_previous_file(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"watermark": "safe"})
    before = path.read_bytes()
    with pytest.raises(ValueError):
        atomic_write_json(path, {"watermark": float("nan")})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_lock_file_is_private_and_symlinks_are_rejected(tmp_path):
    lock = tmp_path / "state.lock"
    with advisory_lock(lock):
        if os.name == "posix":
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600

    target = tmp_path / "other.lock"
    target.touch()
    lock.unlink()
    try:
        lock.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    with pytest.raises((OSError, StateFileError)):
        with advisory_lock(lock):
            pass
