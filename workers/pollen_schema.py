"""Validation and normalization for data crossing scanner trust boundaries."""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

_TYPE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_METADATA_ITEMS = 50
_MAX_METADATA_NODES = 16
_MAX_METADATA_CHARS = 4096


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_text(value: Any, max_length: int, *, multiline: bool = False) -> str:
    """Bound text and remove terminal/control characters from external data."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    chars = []
    for char in value:
        if char in "\r\n\t":
            chars.append(char if multiline else " ")
            continue
        if unicodedata.category(char).startswith("C"):
            continue
        chars.append(char)
    cleaned = "".join(chars)
    if multiline:
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    else:
        cleaned = " ".join(cleaned.split())
    return cleaned[:max_length]


def _clean_timestamp(value: Any) -> str:
    text = clean_text(value, 64)
    if not text:
        return _utc_now_z()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        # Historical backlog is legitimate (for example after a scanner was
        # disabled for several months), so never rewrite an old valid provider
        # timestamp.  Only clamp timestamps that cannot yet have been
        # discovered; this also prevents a malicious far-future value from
        # permanently sorting behind every real item.
        if parsed > now + timedelta(minutes=5):
            return now.strftime("%Y-%m-%dT%H:%M:%SZ")
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OverflowError):
        return _utc_now_z()


def _clean_url(value: Any) -> str:
    raw = value if isinstance(value, str) else str(value or "")
    if any(char.isspace() or unicodedata.category(char).startswith("C") for char in raw):
        return ""
    url = clean_text(raw, 2048)
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or (port is not None and not 1 <= port <= 65535)):
        return ""
    return url


def _clean_json(value: Any, depth: int = 0, budget: list[int] | None = None) -> Any:
    if budget is None:
        # Node and character budgets jointly bound serialized metadata size.
        budget = [_MAX_METADATA_NODES, _MAX_METADATA_CHARS]
    if depth >= 5 or budget[0] <= 0 or budget[1] <= 0:
        return None
    budget[0] -= 1
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value.bit_length() <= 256:
            return value
        text = str(value) if value.bit_length() <= 4096 else "<integer omitted>"
        cleaned = clean_text(text, min(1000, budget[1]), multiline=True)
        budget[1] -= len(cleaned)
        return cleaned
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        cleaned = clean_text(value, min(1000, budget[1]), multiline=True)
        budget[1] -= len(cleaned)
        return cleaned
    if isinstance(value, list):
        result = []
        for item in value[:_MAX_METADATA_ITEMS]:
            if budget[0] <= 0 or budget[1] <= 0:
                break
            result.append(_clean_json(item, depth + 1, budget))
        return result
    if isinstance(value, dict):
        result = {}
        for raw_key, raw_value in list(value.items())[:_MAX_METADATA_ITEMS]:
            if budget[0] <= 0 or budget[1] <= 0:
                break
            key = clean_text(raw_key, min(100, budget[1]))
            if key:
                budget[1] -= len(key)
                result[key] = _clean_json(raw_value, depth + 1, budget)
        return result
    cleaned = clean_text(value, min(1000, budget[1]), multiline=True)
    budget[1] -= len(cleaned)
    return cleaned


def pollen_key(source: Any, pollen_id: Any) -> str:
    """Build the internal dedup key without changing user-visible scanner IDs."""
    return f"{clean_text(source, 64)}\0{clean_text(pollen_id, 256)}"


def normalize_pollen(item: Any, expected_source: str | None = None) -> dict | None:
    """Validate a scanner item and return a bounded canonical pollen object."""
    if not isinstance(item, dict):
        return None

    raw_id = item.get("id")
    if (
        not isinstance(raw_id, str)
        or not raw_id
        or len(raw_id) > 256
        or any(unicodedata.category(char).startswith("C") for char in raw_id)
    ):
        return None
    pollen_id = raw_id

    source = clean_text(expected_source if expected_source is not None else item.get("source"), 64)
    if not source or not _TYPE_RE.fullmatch(source):
        return None

    pollen_type = clean_text(item.get("type"), 64)
    if not _TYPE_RE.fullmatch(pollen_type):
        if expected_source is not None:
            return None
        pollen_type = "notification"

    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    if expected_source is not None:
        metadata = {
            key: value
            for key, value in metadata.items()
            if str(key) not in {"triage_draft", "target_group", "target_group_id"}
        }

    relevance = None if expected_source is not None else item.get("relevance")
    if relevance not in {None, "HIGH", "MEDIUM", "LOW"}:
        relevance = None

    return {
        "id": pollen_id,
        "source": source,
        "type": pollen_type,
        "title": clean_text(item.get("title"), 200),
        "preview": clean_text(item.get("preview"), 500),
        "discovered_at": _clean_timestamp(item.get("discovered_at")),
        "author": clean_text(item.get("author"), 200),
        "author_name": clean_text(item.get("author_name"), 200),
        "group": clean_text(item.get("group"), 200),
        "url": _clean_url(item.get("url")),
        "metadata": _clean_json(metadata),
        "relevance": relevance,
        "relevance_reason": "" if expected_source is not None else clean_text(item.get("relevance_reason"), 500),
        "suggested_action": "" if expected_source is not None else clean_text(item.get("suggested_action"), 500),
    }
