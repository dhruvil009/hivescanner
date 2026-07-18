"""Pollen manager — JSON-based pollen lifecycle: pending -> acknowledged | acted."""

from __future__ import annotations

import json
import math
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from pollen_schema import normalize_pollen, pollen_key
from state_io import advisory_lock, atomic_write_json, load_json

HIVESCANNER_HOME = Path.home() / ".hivescanner"
POLLEN_FILE = HIVESCANNER_HOME / "pollen.json"
CONFIG_FILE = HIVESCANNER_HOME / "config.json"
PENDING_BATCH_FILE = HIVESCANNER_HOME / "pending_batch.json"
POLLEN_LOCK_FILE = HIVESCANNER_HOME / ".pollen.lock"
MAX_QUEUE_ITEMS = 10_000
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> dict:
    hive = load_json(
        POLLEN_FILE,
        {"schema_version": 1, "pollen": [], "last_updated": _utc_now_z()},
        dict,
    )
    if not isinstance(hive.get("pollen", []), list):
        raise RuntimeError(f"Cannot read {POLLEN_FILE}: 'pollen' must be a list")
    hive.setdefault("schema_version", 1)
    hive.setdefault("pollen", [])
    return hive


def save(hive: dict) -> None:
    if not isinstance(hive, dict) or not isinstance(hive.get("pollen"), list):
        raise TypeError("hive must be a dict containing a pollen list")
    hive.setdefault("schema_version", 1)
    hive["last_updated"] = _utc_now_z()
    atomic_write_json(POLLEN_FILE, hive)


def add_pollen(hive: dict, pollen: list[dict]) -> list[dict]:
    """Add pollen to hive, dedup by ID. Returns list of newly added pollen."""
    hive.setdefault("pollen", [])
    existing_ids = {
        pollen_key(p.get("source"), p.get("id"))
        for p in hive["pollen"]
        if isinstance(p, dict) and p.get("id")
    }
    added = []
    for raw_pollen in pollen:
        if len(hive["pollen"]) >= MAX_QUEUE_ITEMS:
            print(
                f"[pollen] queue reached hard limit ({MAX_QUEUE_ITEMS}); refusing additional items",
                file=sys.stderr,
            )
            break
        p = normalize_pollen(raw_pollen)
        if p is None:
            source = raw_pollen.get("source", "unknown") if isinstance(raw_pollen, dict) else "unknown"
            title = raw_pollen.get("title", "") if isinstance(raw_pollen, dict) else ""
            reason = "item without 'id'" if isinstance(raw_pollen, dict) and not raw_pollen.get("id") else "invalid item"
            print(
                f"[pollen] skipping {reason} from source={source}: {str(title)[:60]}",
                file=sys.stderr,
            )
            continue
        key = pollen_key(p["source"], p["id"])
        if key in existing_ids:
            continue
        # Scanner-controlled input cannot set lifecycle state.
        p["status"] = "pending"
        p["surfaced_count"] = 0
        p["acknowledged_at"] = None
        p["acted_at"] = None
        hive["pollen"].append(p)
        existing_ids.add(key)
        added.append(p)
    return added


def get_pending(hive: dict) -> list[dict]:
    """Return pending pollen sorted by discovered_at."""
    pending = [p for p in hive.get("pollen", []) if isinstance(p, dict) and p.get("status") == "pending"]

    def sort_key(item: dict) -> tuple[datetime, str, str]:
        raw = str(item.get("discovered_at") or "")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            parsed = datetime.max.replace(tzinfo=timezone.utc)
        return parsed, str(item.get("source") or ""), str(item.get("id") or "")

    pending.sort(key=sort_key)
    return pending


def dismiss(hive: dict, pollen_ids: list[str]) -> int:
    """Mark pollen as acknowledged by ID. Returns count dismissed."""
    ids_set = set(pollen_ids)
    count = 0
    for p in hive.get("pollen", []):
        if isinstance(p, dict) and p.get("id") in ids_set and p.get("status") == "pending":
            p["status"] = "acknowledged"
            p["acknowledged_at"] = _utc_now_z()
            count += 1
    return count


def dismiss_by_number(hive: dict, numbers: list[int]) -> int:
    """Dismiss pending pollen by 1-indexed display number.
    Ordering matches get_pending() — sorted by discovered_at."""
    pending = get_pending(hive)
    count = 0
    for n in numbers:
        idx = n - 1
        if 0 <= idx < len(pending):
            item = pending[idx]
            if item.get("status") == "pending":
                item["status"] = "acknowledged"
                item["acknowledged_at"] = _utc_now_z()
                count += 1
    return count


def dismiss_all(hive: dict) -> int:
    """Acknowledge all pending pollen."""
    count = 0
    for p in hive.get("pollen", []):
        if isinstance(p, dict) and p.get("status") == "pending":
            p["status"] = "acknowledged"
            p["acknowledged_at"] = _utc_now_z()
            count += 1
    return count


def mark_acted(hive: dict, pollen_ids: list[str]) -> int:
    """Mark pollen as acted (externally handled). Returns count."""
    ids_set = set(pollen_ids)
    count = 0
    for p in hive.get("pollen", []):
        if isinstance(p, dict) and p.get("id") in ids_set and p.get("status") == "pending":
            p["status"] = "acted"
            p["acted_at"] = _utc_now_z()
            count += 1
    return count


def mark_acted_refs(hive: dict, references: list[object]) -> int:
    """Mark exact source/ID references, while accepting legacy bare IDs."""
    qualified = {
        pollen_key(value.get("source"), value.get("id"))
        for value in references
        if isinstance(value, dict) and value.get("source") and value.get("id")
    }
    legacy_ids = {
        value for value in references if isinstance(value, str) and value
    }
    count = 0
    for item in hive.get("pollen", []):
        if not isinstance(item, dict) or item.get("status") != "pending":
            continue
        if (
            pollen_key(item.get("source"), item.get("id")) not in qualified
            and item.get("id") not in legacy_ids
        ):
            continue
        item["status"] = "acted"
        item["acted_at"] = _utc_now_z()
        count += 1
    return count


def increment_surfaced(hive: dict, ids: list[str]) -> None:
    """Track how many times pollen has been shown."""
    ids_set = set(ids)
    for p in hive.get("pollen", []):
        if isinstance(p, dict) and p.get("id") in ids_set:
            current = p.get("surfaced_count", 0)
            p["surfaced_count"] = (current if isinstance(current, int) and not isinstance(current, bool) else 0) + 1


def increment_surfaced_by_number(hive: dict, numbers: list[int]) -> int:
    """Increment counters using only local display numbers, never external IDs."""
    pending = get_pending(hive)
    count = 0
    for number in set(numbers):
        index = number - 1
        if not 0 <= index < len(pending):
            continue
        item = pending[index]
        current = item.get("surfaced_count", 0)
        item["surfaced_count"] = (
            current
            if isinstance(current, int) and not isinstance(current, bool)
            else 0
        ) + 1
        count += 1
    return count


def consume_pending_batch() -> dict:
    """Atomically import the scanner's durable handoff without shell JSON.

    The Queen invokes this no-argument operation after receiving a scanner-loop
    notification. External message text and IDs therefore never become shell
    arguments. The scanner loop independently verifies the import before it
    commits source watermarks.
    """
    with advisory_lock(POLLEN_LOCK_FILE):
        if not PENDING_BATCH_FILE.exists():
            return {"error": "No pending scanner batch exists"}
        batch = load_json(PENDING_BATCH_FILE, {}, dict)
        raw_pollen = batch.get("pollen", [])
        acted_ids = batch.get("acted_ids", [])
        if not isinstance(raw_pollen, list) or not isinstance(acted_ids, list):
            raise RuntimeError(
                f"Cannot read {PENDING_BATCH_FILE}: pollen and acted_ids must be lists"
            )
        if len(raw_pollen) > MAX_QUEUE_ITEMS or len(acted_ids) > MAX_QUEUE_ITEMS:
            raise RuntimeError(f"Cannot read {PENDING_BATCH_FILE}: batch is too large")

        def valid_id(value: object) -> bool:
            return (
                isinstance(value, str)
                and 0 < len(value) <= 256
                and not any(
                    unicodedata.category(char).startswith("C") for char in value
                )
            )

        if not all(
            valid_id(value)
            or (
                isinstance(value, dict)
                and isinstance(value.get("source"), str)
                and _SOURCE_RE.fullmatch(value["source"]) is not None
                and valid_id(value.get("id"))
            )
            for value in acted_ids
        ):
            raise RuntimeError(
                f"Cannot read {PENDING_BATCH_FILE}: acted_ids entries are invalid"
            )

        normalized_pollen = []
        for raw_item in raw_pollen:
            source = raw_item.get("source") if isinstance(raw_item, dict) else None
            if not isinstance(source, str) or _SOURCE_RE.fullmatch(source) is None:
                raise RuntimeError(
                    f"Cannot read {PENDING_BATCH_FILE}: pollen source is invalid"
                )
            item = normalize_pollen(raw_item, expected_source=source)
            if item is None:
                raise RuntimeError(
                    f"Cannot read {PENDING_BATCH_FILE}: pollen item is invalid"
                )
            normalized_pollen.append(item)

        hive = load()
        added = add_pollen(hive, normalized_pollen)
        acted = mark_acted_refs(hive, acted_ids)
        save(hive)
    return {
        "added": len(added),
        "acted": acted,
        "delivered": len(raw_pollen),
    }


def prune(hive: dict, retention_days: int = 7) -> int:
    """Remove old acknowledged/acted pollen. NEVER prune pending."""
    if isinstance(retention_days, bool) or not isinstance(retention_days, (int, float)):
        raise ValueError("retention_days must be a non-negative number")
    if not math.isfinite(retention_days) or retention_days < 0:
        raise ValueError("retention_days must be a non-negative number")
    now = datetime.now(timezone.utc)
    cutoff_seconds = retention_days * 86400
    original_count = len(hive.get("pollen", []))
    kept = []
    for p in hive.get("pollen", []):
        if not isinstance(p, dict):
            kept.append(p)
            continue
        if p.get("status") == "pending":
            kept.append(p)
            continue
        ts_str = p.get("acknowledged_at") or p.get("acted_at") or p.get("discovered_at", "")
        if not ts_str:
            kept.append(p)
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (now - ts).total_seconds() < cutoff_seconds:
                kept.append(p)
        except ValueError:
            kept.append(p)
    hive["pollen"] = kept
    return original_count - len(kept)


def stats(hive: dict) -> dict:
    """Counts of total, pending, acknowledged, acted."""
    counts = {"total": 0, "pending": 0, "acknowledged": 0, "acted": 0}
    for p in hive.get("pollen", []):
        if not isinstance(p, dict):
            continue
        counts["total"] += 1
        status = p.get("status", "pending")
        if status in counts:
            counts[status] += 1
    return counts


def load_pollen_ids() -> set[str]:
    """Load ALL pollen IDs from hive — pending, acknowledged, and acted.
    Prevents re-reporting dismissed pollen on the next cycle."""
    hive = load()
    return {
        p["id"]
        for p in hive.get("pollen", [])
        if isinstance(p, dict) and p.get("id")
    }


def load_pollen_keys() -> set[str]:
    """Load source-qualified IDs for collision-safe scanner deduplication."""
    hive = load()
    return {
        pollen_key(p.get("source"), p.get("id"))
        for p in hive.get("pollen", [])
        if isinstance(p, dict) and p.get("id")
    }


# --- CLI interface ---

def _cli_get_pending():
    hive = load()
    pending = get_pending(hive)
    print(json.dumps(pending, indent=2))


def _cli_dismiss(args: list[str]):
    try:
        numbers = [int(a) for a in args]
    except ValueError:
        print(json.dumps({"error": "dismiss requires integer arguments"}))
        sys.exit(1)
    with advisory_lock(POLLEN_LOCK_FILE):
        hive = load()
        count = dismiss_by_number(hive, numbers)
        save(hive)
    print(json.dumps({"dismissed": count}))


def _cli_dismiss_all():
    with advisory_lock(POLLEN_LOCK_FILE):
        hive = load()
        count = dismiss_all(hive)
        save(hive)
    print(json.dumps({"dismissed": count}))


def _cli_add_pollen(json_str: str):
    try:
        pollen = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        sys.exit(1)
    if not isinstance(pollen, list):
        print(json.dumps({"error": "add_pollen requires a JSON array"}))
        sys.exit(1)
    with advisory_lock(POLLEN_LOCK_FILE):
        hive = load()
        added = add_pollen(hive, pollen)
        save(hive)
    print(json.dumps({"added": len(added)}))


def _cli_stats():
    hive = load()
    print(json.dumps(stats(hive), indent=2))


def _cli_prune():
    retention_days = 7
    if CONFIG_FILE.exists():
        config = load_json(CONFIG_FILE, {}, dict)
        retention_days = config.get("queue_retention_days", 7)
    with advisory_lock(POLLEN_LOCK_FILE):
        hive = load()
        pruned = prune(hive, retention_days=retention_days)
        save(hive)
    print(json.dumps({"pruned": pruned}))


def _cli_mark_acted(args: list[str]):
    with advisory_lock(POLLEN_LOCK_FILE):
        hive = load()
        count = mark_acted(hive, args)
        save(hive)
    print(json.dumps({"acted": count}))


def _cli_increment_surfaced(args: list[str]):
    with advisory_lock(POLLEN_LOCK_FILE):
        hive = load()
        increment_surfaced(hive, args)
        save(hive)
    print(json.dumps({"surfaced": len(args)}))


def _cli_increment_surfaced_numbers(args: list[str]):
    try:
        numbers = [int(value) for value in args]
    except ValueError:
        print(json.dumps({"error": "increment_surfaced_numbers requires integers"}))
        sys.exit(1)
    with advisory_lock(POLLEN_LOCK_FILE):
        hive = load()
        count = increment_surfaced_by_number(hive, numbers)
        save(hive)
    print(json.dumps({"surfaced": count}))


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
    elif cmd in {"add_pollen", "mark_acted", "increment_surfaced"}:
        print(json.dumps({
            "error": (
                f"Unsafe argv command '{cmd}' is disabled; use "
                "consume_pending_batch or a number-based command"
            )
        }))
        sys.exit(1)
    elif cmd == "consume_pending_batch":
        print(json.dumps(consume_pending_batch()))
    elif cmd == "stats":
        _cli_stats()
    elif cmd == "prune":
        _cli_prune()
    elif cmd == "increment_surfaced_numbers":
        _cli_increment_surfaced_numbers(sys.argv[2:])
    else:
        print(json.dumps({"error": f"Unknown command: {cmd}"}))
        sys.exit(1)
