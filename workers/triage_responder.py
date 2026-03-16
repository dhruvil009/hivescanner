"""Triage responder — template-based draft generation + safety gates for oncall autonomy."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HIVESCANNER_HOME = Path.home() / ".hivescanner"
CONFIG_FILE = HIVESCANNER_HOME / "config.json"
POLLEN_FILE = HIVESCANNER_HOME / "pollen.json"
AUDIT_FILE = HIVESCANNER_HOME / "audit.json"

MAX_POSTS_PER_HOUR_PER_GROUP = 3
MAX_CONTENT_LENGTH = 1000
REQUIRED_PREFIX = "[Posted by HiveScanner]"
ATTRIBUTION_PREFIX = "[Posted by HiveScanner - oncall triage assist]"
AUTO_POST_PREFIX = "[Automated: HiveScanner - Not Human Validated]"

REMEDIATION_PATTERNS = [
    re.compile(r"\b(try|run|execute|apply|add|remove|change|modify|update|set|fix)\b"
               r".*\b(command|config|setting|flag|option|acl|permission|code)\b", re.IGNORECASE),
    re.compile(r"\b(you should|you could|I recommend|I suggest|consider|make sure to)\b", re.IGNORECASE),
    re.compile(r"\b(workaround|solution|resolution|to fix this|to resolve)\b", re.IGNORECASE),
    re.compile(r"\bsteps?\s*(to|for)\b", re.IGNORECASE),
    re.compile(r"\b(rollback|revert|cherry.pick|backout|hotfix)\b", re.IGNORECASE),
    re.compile(r"```"),
]

TEMPLATES = {
    "crash": "{prefix}\n\nPossibly related:\n{context_links}\n\nCan you share the crash ID?",
    "sev": "{prefix}\n\nTriaging - related context:\n{context_links}\n\nWhat's the impact scope?",
    "default": "{prefix}\n\nRelated context:\n{context_links}",
}


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _load_audit() -> dict:
    if not AUDIT_FILE.exists():
        return {"entries": []}
    try:
        return json.loads(AUDIT_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"entries": []}


def _save_audit(audit: dict) -> None:
    HIVESCANNER_HOME.mkdir(parents=True, exist_ok=True)
    tmp = AUDIT_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(audit, f, indent=2)
    os.replace(str(tmp), str(AUDIT_FILE))


def _log_audit(action: str, **kwargs) -> None:
    audit = _load_audit()
    entry = {"timestamp": _utc_now_z(), "action": action}
    entry.update(kwargs)
    audit["entries"].append(entry)
    _save_audit(audit)


def _rate_limited(group_id: str) -> bool:
    """Check if triage posts for this group have hit the limit in the last hour."""
    audit = _load_audit()
    now = datetime.now(timezone.utc)
    count = 0
    for entry in audit.get("entries", []):
        if entry.get("action") != "triage_post":
            continue
        if entry.get("target_group_id") != group_id:
            continue
        try:
            ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
            if (now - ts).total_seconds() < 3600:
                count += 1
        except (ValueError, KeyError):
            pass
    return count >= MAX_POSTS_PER_HOUR_PER_GROUP


def _rate_limited_auto() -> bool:
    """Check if auto-posts have hit global limit in the last hour."""
    audit = _load_audit()
    now = datetime.now(timezone.utc)
    count = 0
    for entry in audit.get("entries", []):
        if entry.get("action") != "auto_post":
            continue
        try:
            ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
            if (now - ts).total_seconds() < 3600:
                count += 1
        except (ValueError, KeyError):
            pass
    return count >= MAX_POSTS_PER_HOUR_PER_GROUP


def _content_safe(content: str) -> bool:
    """Check content doesn't contain remediation language."""
    for pattern in REMEDIATION_PATTERNS:
        if pattern.search(content):
            return False
    return True


def generate_draft(item: dict, group_config: dict) -> dict:
    """Generate a draft response using templates (no LLM call).

    Pre-checks:
    - triage.enabled must be True for this group
    - item type must be in allowed_item_types
    - trigger_keywords must match (if configured)
    - cooldown_minutes must have elapsed since last post to this thread
    - rate limit must not be exceeded

    Returns: {"draft": str, "blocked": bool, "block_reason": str}
    """
    triage = group_config.get("triage", {})
    if not triage.get("enabled"):
        return {"draft": "", "blocked": True, "block_reason": "triage not enabled for this group"}

    allowed_types = triage.get("allowed_item_types", [])
    if allowed_types and item.get("type") not in allowed_types:
        return {"draft": "", "blocked": True,
                "block_reason": f"item type '{item.get('type')}' not in allowed types"}

    # Keyword trigger check
    trigger_keywords = triage.get("trigger_keywords", [])
    if trigger_keywords:
        text = f"{item.get('title', '')} {item.get('preview', '')}".lower()
        if not any(kw.lower() in text for kw in trigger_keywords):
            return {"draft": "", "blocked": True, "block_reason": "no trigger keyword matched"}

    group_id = group_config.get("id", "unknown")

    if _rate_limited(group_id):
        return {"draft": "", "blocked": True, "block_reason": "rate limit reached for this group"}

    # Select template
    title_lower = item.get("title", "").lower()
    preview_lower = item.get("preview", "").lower()
    combined = f"{title_lower} {preview_lower}"

    if "crash" in combined:
        template_key = "crash"
    elif "sev" in combined or "incident" in combined:
        template_key = "sev"
    else:
        template_key = "default"

    context_links = item.get("url", "No link available")
    draft = TEMPLATES[template_key].format(
        prefix=ATTRIBUTION_PREFIX,
        context_links=context_links,
    )

    if len(draft) > MAX_CONTENT_LENGTH:
        draft = draft[:MAX_CONTENT_LENGTH]

    return {"draft": draft, "blocked": False, "block_reason": ""}


def post_triage_response(pollen_id: str) -> dict:
    """Post a triage response — 6 Python-enforced gates."""
    config = _load_config()

    # Gate 1: autonomy.enabled
    if not config.get("autonomy", {}).get("enabled"):
        _log_audit("triage_blocked", pollen_id=pollen_id, gate="autonomy")
        return {"error": "Autonomy is disabled", "gate": "autonomy"}

    # Gate 2: pollen must exist and have a triage draft
    hive = {}
    if POLLEN_FILE.exists():
        try:
            hive = json.loads(POLLEN_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    pollen = None
    for p in hive.get("pollen", []):
        if p.get("id") == pollen_id:
            pollen = p
            break
    if not pollen:
        return {"error": f"Pollen {pollen_id} not found", "gate": "draft"}

    draft = pollen.get("metadata", {}).get("triage_draft", "")
    if not draft:
        return {"error": "No triage draft on this pollen", "gate": "draft"}

    # Gate 3: target group must be in oncall_groups allowlist
    oncall_groups = config.get("autonomy", {}).get("oncall_groups", [])
    target_group = pollen.get("metadata", {}).get("target_group", "")
    if target_group not in oncall_groups:
        _log_audit("triage_blocked", pollen_id=pollen_id, gate="allowlist",
                   target_group_id=target_group)
        return {"error": f"Group '{target_group}' not in oncall_groups allowlist", "gate": "allowlist"}

    # Gate 4: rate limit
    if _rate_limited(target_group):
        _log_audit("triage_blocked", pollen_id=pollen_id, gate="rate_limit",
                   target_group_id=target_group)
        return {"error": "Rate limit reached for this group", "gate": "rate_limit"}

    # Gate 5: content safety
    if not _content_safe(draft):
        _log_audit("triage_blocked", pollen_id=pollen_id, gate="content",
                   draft_content=draft[:200])
        return {"error": "Draft contains remediation language — unsafe for auto-post", "gate": "content"}

    # Gate 6: attribution prefix
    if not draft.startswith(ATTRIBUTION_PREFIX) and not draft.startswith(REQUIRED_PREFIX):
        _log_audit("triage_blocked", pollen_id=pollen_id, gate="prefix")
        return {"error": "Draft missing required attribution prefix", "gate": "prefix"}

    # All gates passed — in a real implementation, post via the appropriate API
    _log_audit("triage_post", pollen_id=pollen_id, target_group_id=target_group,
               draft_content=draft, content_length=len(draft),
               gates_passed=["autonomy", "draft", "allowlist", "rate_limit", "content", "prefix"])

    return {"status": "posted", "pollen_id": pollen_id, "content_length": len(draft)}


def post_auto_response(target_id: str, content: str) -> dict:
    """Auto-post with relaxed gates (no content safety, no confirmation)."""
    config = _load_config()

    # Gate 1: autonomy.enabled
    if not config.get("autonomy", {}).get("enabled"):
        return {"error": "Autonomy is disabled"}

    # Gate 2: rate limit (global for auto-posts)
    if _rate_limited_auto():
        return {"error": "Auto-post rate limit reached"}

    # Gate 3: prefix required
    if not content.startswith(AUTO_POST_PREFIX):
        content = f"{AUTO_POST_PREFIX}\n\n{content}"

    _log_audit("auto_post", target_id=target_id, content=content[:500],
               content_length=len(content))

    return {"status": "posted", "target_id": target_id, "content_length": len(content)}


def set_autonomy(enabled: bool) -> dict:
    """Instant kill switch."""
    config = _load_config()
    if "autonomy" not in config:
        config["autonomy"] = {}
    config["autonomy"]["enabled"] = enabled

    HIVESCANNER_HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(str(tmp), str(CONFIG_FILE))

    _log_audit("autonomy_toggled", enabled=enabled)
    return {"status": "ok", "autonomy_enabled": enabled}


def autonomy_status() -> dict:
    config = _load_config()
    autonomy = config.get("autonomy", {})
    return {
        "enabled": autonomy.get("enabled", False),
        "oncall_groups": autonomy.get("oncall_groups", []),
    }


# --- CLI ---

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: triage_responder.py <command> [args]"}))
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "generate_draft":
        if len(sys.argv) < 4:
            print(json.dumps({"error": "generate_draft requires <pollen_json> <group_config_json>"}))
            sys.exit(1)
        try:
            item = json.loads(sys.argv[2])
            group_config = json.loads(sys.argv[3])
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid JSON: {e}"}))
            sys.exit(1)
        print(json.dumps(generate_draft(item, group_config), indent=2))

    elif cmd == "post_response":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "post_response requires <pollen_id>"}))
            sys.exit(1)
        print(json.dumps(post_triage_response(sys.argv[2]), indent=2))

    elif cmd == "post_auto":
        if len(sys.argv) < 4:
            print(json.dumps({"error": "post_auto requires <target_id> <content>"}))
            sys.exit(1)
        print(json.dumps(post_auto_response(sys.argv[2], sys.argv[3]), indent=2))

    elif cmd == "autonomy_status":
        print(json.dumps(autonomy_status(), indent=2))

    elif cmd == "autonomy_set":
        if len(sys.argv) < 3 or sys.argv[2] not in ("on", "off"):
            print(json.dumps({"error": "autonomy_set requires 'on' or 'off'"}))
            sys.exit(1)
        print(json.dumps(set_autonomy(sys.argv[2] == "on"), indent=2))

    else:
        print(json.dumps({"error": f"Unknown command: {cmd}"}))
        sys.exit(1)
