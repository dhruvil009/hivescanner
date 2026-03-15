"""Snapshot store — persists scanner state to disk across process restarts."""

import json
import os
from pathlib import Path

HIVESCANNER_HOME = Path.home() / ".hivescanner"
SNAPSHOTS_FILE = HIVESCANNER_HOME / "snapshots.json"


def _load_all() -> dict:
    if not SNAPSHOTS_FILE.exists():
        return {}
    try:
        return json.loads(SNAPSHOTS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all(data: dict) -> None:
    HIVESCANNER_HOME.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOTS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(str(tmp), str(SNAPSHOTS_FILE))


def load_snapshot(name: str) -> dict:
    """Load named snapshot. Returns {} if missing."""
    return _load_all().get(name, {})


def save_snapshot(name: str, snapshot: dict) -> None:
    """Save named snapshot. Merges with existing snapshots on disk."""
    all_snapshots = _load_all()
    all_snapshots[name] = snapshot
    _save_all(all_snapshots)
