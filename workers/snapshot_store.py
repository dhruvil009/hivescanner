"""Snapshot store — persists scanner state to disk across process restarts."""

from __future__ import annotations

from pathlib import Path

from state_io import atomic_write_json, load_json

HIVESCANNER_HOME = Path.home() / ".hivescanner"
SNAPSHOTS_FILE = HIVESCANNER_HOME / "snapshots.json"


def _load_all() -> dict:
    return load_json(SNAPSHOTS_FILE, {}, dict)


def _save_all(data: dict) -> None:
    atomic_write_json(SNAPSHOTS_FILE, data)


def load_snapshot(name: str) -> dict:
    """Load named snapshot. Returns {} if missing."""
    snapshot = _load_all().get(name, {})
    if not isinstance(snapshot, dict):
        raise RuntimeError(f"Snapshot '{name}' must be a JSON object")
    return snapshot


def snapshot_exists(name: str) -> bool:
    """Return whether a snapshot was initialized, even when its value is empty."""
    return name in _load_all()


def save_snapshot(name: str, snapshot: dict) -> None:
    """Save named snapshot. Merges with existing snapshots on disk."""
    if not isinstance(snapshot, dict):
        raise TypeError("snapshot must be a dict")
    all_snapshots = _load_all()
    all_snapshots[name] = snapshot
    _save_all(all_snapshots)
