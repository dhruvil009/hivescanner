"""Pollen manager — JSON-based pollen lifecycle: pending -> acknowledged | acted."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HIVESCANNER_HOME = Path.home() / ".hivescanner"
POLLEN_FILE = HIVESCANNER_HOME / "pollen.json"


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> dict:
    if not POLLEN_FILE.exists():
        return {"pollen": [], "last_updated": _utc_now_z()}
    try:
        return json.loads(POLLEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"pollen": [], "last_updated": _utc_now_z()}


def save(hive: dict) -> None:
    HIVESCANNER_HOME.mkdir(parents=True, exist_ok=True)
    hive["last_updated"] = _utc_now_z()
    tmp = POLLEN_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(hive, f, indent=2)
    os.replace(str(tmp), str(POLLEN_FILE))


def add_pollen(hive: dict, pollen: list[dict]) -> list[dict]:
    """Add pollen to hive, dedup by ID. Returns list of newly added pollen."""
    existing_ids = {p["id"] for p in hive["pollen"] if p.get("id")}
    added = []
    for p in pollen:
        if p["id"] in existing_ids:
            continue
        p.setdefault("status", "pending")
        p.setdefault("surfaced_count", 0)
        p.setdefault("relevance", None)
        p.setdefault("relevance_reason", None)
        p.setdefault("suggested_action", None)
        p.setdefault("acknowledged_at", None)
        p.setdefault("acted_at", None)
        hive["pollen"].append(p)
        existing_ids.add(p["id"])
        added.append(p)
    return added


def get_pending(hive: dict) -> list[dict]:
    """Return pending pollen sorted by discovered_at."""
    pending = [p for p in hive["pollen"] if p.get("status") == "pending"]
    pending.sort(key=lambda p: p.get("discovered_at", ""))
    return pending


def dismiss(hive: dict, pollen_ids: list[str]) -> int:
    """Mark pollen as acknowledged by ID. Returns count dismissed."""
    ids_set = set(pollen_ids)
    count = 0
    for p in hive["pollen"]:
        if p["id"] in ids_set and p.get("status") == "pending":
            p["status"] = "acknowledged"
            p["acknowledged_at"] = _utc_now_z()
            count += 1
    return count


def dismiss_by_number(hive: dict, numbers: list[int]) -> int:
    """Dismiss pending pollen by 1-indexed display number.
    Ordering matches get_pending() — sorted by discovered_at."""
    pending = get_pending(hive)
    ids_to_dismiss = []
    for n in numbers:
        idx = n - 1
        if 0 <= idx < len(pending):
            ids_to_dismiss.append(pending[idx]["id"])
    return dismiss(hive, ids_to_dismiss)


def dismiss_all(hive: dict) -> int:
    """Acknowledge all pending pollen."""
    count = 0
    for p in hive["pollen"]:
        if p.get("status") == "pending":
            p["status"] = "acknowledged"
            p["acknowledged_at"] = _utc_now_z()
            count += 1
    return count


def mark_acted(hive: dict, pollen_ids: list[str]) -> int:
    """Mark pollen as acted (externally handled). Returns count."""
    ids_set = set(pollen_ids)
    count = 0
    for p in hive["pollen"]:
        if p["id"] in ids_set and p.get("status") == "pending":
            p["status"] = "acted"
            p["acted_at"] = _utc_now_z()
            count += 1
    return count


def increment_surfaced(hive: dict, ids: list[str]) -> None:
    """Track how many times pollen has been shown."""
    ids_set = set(ids)
    for p in hive["pollen"]:
        if p["id"] in ids_set:
            p["surfaced_count"] = p.get("surfaced_count", 0) + 1


def prune(hive: dict, retention_days: int = 7) -> int:
    """Remove old acknowledged/acted pollen. NEVER prune pending."""
    now = datetime.now(timezone.utc)
    cutoff_seconds = retention_days * 86400
    original_count = len(hive["pollen"])
    kept = []
    for p in hive["pollen"]:
        if p.get("status") == "pending":
            kept.append(p)
            continue
        ts_str = p.get("acknowledged_at") or p.get("acted_at") or p.get("discovered_at", "")
        if not ts_str:
            kept.append(p)
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if (now - ts).total_seconds() < cutoff_seconds:
                kept.append(p)
        except ValueError:
            kept.append(p)
    hive["pollen"] = kept
    return original_count - len(kept)


def stats(hive: dict) -> dict:
    """Counts of total, pending, acknowledged, acted."""
    counts = {"total": 0, "pending": 0, "acknowledged": 0, "acted": 0}
    for p in hive["pollen"]:
        counts["total"] += 1
        status = p.get("status", "pending")
        if status in counts:
            counts[status] += 1
    return counts


def load_pollen_ids() -> set[str]:
    """Load ALL pollen IDs from hive — pending, acknowledged, and acted.
    Prevents re-reporting dismissed pollen on the next cycle."""
    hive = load()
    return {p["id"] for p in hive.get("pollen", []) if p.get("id")}


# --- CLI interface ---

def _cli_get_pending():
    hive = load()
    pending = get_pending(hive)
    print(json.dumps(pending, indent=2))


def _cli_dismiss(args: list[str]):
    hive = load()
    try:
        numbers = [int(a) for a in args]
    except ValueError:
        print(json.dumps({"error": "dismiss requires integer arguments"}))
        sys.exit(1)
    count = dismiss_by_number(hive, numbers)
    save(hive)
    print(json.dumps({"dismissed": count}))


def _cli_dismiss_all():
    hive = load()
    count = dismiss_all(hive)
    save(hive)
    print(json.dumps({"dismissed": count}))


def _cli_add_pollen(json_str: str):
    hive = load()
    try:
        pollen = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        sys.exit(1)
    added = add_pollen(hive, pollen)
    save(hive)
    print(json.dumps({"added": len(added)}))


def _cli_stats():
    hive = load()
    print(json.dumps(stats(hive), indent=2))


def _cli_prune():
    hive = load()
    pruned = prune(hive)
    save(hive)
    print(json.dumps({"pruned": pruned}))


def _cli_mark_acted(args: list[str]):
    hive = load()
    count = mark_acted(hive, args)
    save(hive)
    print(json.dumps({"acted": count}))


def _cli_increment_surfaced(args: list[str]):
    hive = load()
    increment_surfaced(hive, args)
    save(hive)
    print(json.dumps({"surfaced": len(args)}))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: pollen_manager.py <command> [args]"}))
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "get_pending":
        _cli_get_pending()
    elif cmd == "dismiss":
        _cli_dismiss(sys.argv[2:])
    elif cmd == "dismiss_all":
        _cli_dismiss_all()
    elif cmd == "add_pollen":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "add_pollen requires JSON argument"}))
            sys.exit(1)
        _cli_add_pollen(sys.argv[2])
    elif cmd == "stats":
        _cli_stats()
    elif cmd == "prune":
        _cli_prune()
    elif cmd == "mark_acted":
        _cli_mark_acted(sys.argv[2:])
    elif cmd == "increment_surfaced":
        _cli_increment_surfaced(sys.argv[2:])
    else:
        print(json.dumps({"error": f"Unknown command: {cmd}"}))
        sys.exit(1)
