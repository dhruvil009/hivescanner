"""Template-based triage drafts and fail-closed, allowlisted posting."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pollen_manager import get_pending
from state_io import StateFileError, advisory_lock, atomic_write_json, load_json


HIVESCANNER_HOME = Path.home() / ".hivescanner"
CONFIG_FILE = HIVESCANNER_HOME / "config.json"
POLLEN_FILE = HIVESCANNER_HOME / "pollen.json"
AUDIT_FILE = HIVESCANNER_HOME / "audit.json"
AUDIT_LOCK_FILE = HIVESCANNER_HOME / ".triage.lock"
CONFIG_LOCK_FILE = HIVESCANNER_HOME / ".config.lock"
DRAFTS_DIR = HIVESCANNER_HOME / "drafts"

MAX_POSTS_PER_HOUR_PER_GROUP = 3
MAX_ATTEMPTS_PER_HOUR_PER_GROUP = 6
MAX_CONTENT_LENGTH = 1000
MAX_AUDIT_ENTRIES = 10_000
MAX_TICKET_AGE_MINUTES = 60
REQUIRED_PREFIX = "[Posted by HiveScanner]"
ATTRIBUTION_PREFIX = "[Posted by HiveScanner - oncall triage assist]"
AUTO_POST_PREFIX = "[Automated: HiveScanner - Not Human Validated]"
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TICKET_RE = re.compile(r"^[a-f0-9]{32}$")
_SLACK_CHANNEL_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
_SLACK_TS_RE = re.compile(r"^\d{1,20}\.\d{1,20}$")
_DEFAULT_LINK_HOSTS = {
    "slack": {"slack.com", "app.slack.com"},
    "github": {"github.com"},
    "email": {"mail.google.com"},
    "gchat": {"chat.google.com"},
    "calendar": {"calendar.google.com"},
    "whatsapp": {"web.whatsapp.com"},
}

REMEDIATION_PATTERNS = [
    re.compile(r"\b(try|run|execute|apply|add|remove|change|modify|update|set|fix)\b"
               r".*\b(command|config|setting|flag|option|acl|permission|code)\b", re.IGNORECASE),
    re.compile(r"\b(you should|you could|I recommend|I suggest|consider|make sure to)\b", re.IGNORECASE),
    re.compile(r"\b(workaround|solution|resolution|to fix this|to resolve)\b", re.IGNORECASE),
    re.compile(r"\bsteps?\s*(to|for)\b", re.IGNORECASE),
    re.compile(r"\b(rollback|revert|cherry.pick|backout|hotfix)\b", re.IGNORECASE),
    re.compile(r"```"),
    re.compile(r"\b(restart|reboot|bounce|roll|rollout|redeploy|deploy|scale|drain|"
               r"cordon|evict|kill|terminate|toggle|flip|disable|enable|patch|hotpatch)\b"
               r".*\b(service|services|pod|pods|container|containers|deployment|deployments|"
               r"node|nodes|cluster|clusters|job|jobs|worker|workers|db|database|databases|"
               r"server|servers|instance|instances|app|apps|build|release|prod|production|"
               r"staging|canary|flag|flags|feature)\b", re.IGNORECASE),
    re.compile(r"\b(ssh|log\s*in(?:to)?|login|shell\s*into|exec\s*into)\b"
               r".*\b(prod|production|server|servers|box|host|node|instance|db|database|pod|container)\b",
               re.IGNORECASE),
    re.compile(r"\b(grep|tail|kubectl|docker|helm|terraform|systemctl|journalctl|aws|gcloud)\b",
               re.IGNORECASE),
]

TEMPLATES = {
    "crash": "{prefix}\n\nPossibly related:\n{context_links}\n\nCan you share the crash ID?",
    "sev": "{prefix}\n\nTriaging - related context:\n{context_links}\n\nWhat's the impact scope?",
    "default": "{prefix}\n\nRelated context:\n{context_links}",
}


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a triage transport credential to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        try:
            source_port = source.port or (443 if source.scheme == "https" else 80)
            target_port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            source_port, target_port = -2, -1
        if (
            source.scheme.casefold() != target.scheme.casefold()
            or (source.hostname or "").casefold()
            != (target.hostname or "").casefold()
            or source_port != target_port
        ):
            raise urllib.error.HTTPError(
                newurl, code, "cross-origin redirect refused", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _transport_urlopen(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_SameOriginRedirectHandler()).open(
        request, timeout=timeout
    )


def _strict_response_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_response_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_config() -> dict:
    return load_json(CONFIG_FILE, {}, dict)


def _load_audit() -> dict:
    audit = load_json(AUDIT_FILE, {"schema_version": 1, "entries": []}, dict)
    if not isinstance(audit.get("entries", []), list):
        raise StateFileError(f"Cannot read {AUDIT_FILE}: entries must be a list")
    audit.setdefault("schema_version", 1)
    return audit


def _audit_lock():
    return advisory_lock(AUDIT_LOCK_FILE)


def _save_audit(audit: dict) -> None:
    entries = audit.get("entries", [])
    if not isinstance(entries, list):
        raise TypeError("audit entries must be a list")
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    retained = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            timestamp = datetime.fromisoformat(str(entry.get("timestamp", "")).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if timestamp >= cutoff:
                retained.append(entry)
        except (TypeError, ValueError):
            retained.append(entry)
    audit["entries"] = retained[-MAX_AUDIT_ENTRIES:]
    atomic_write_json(AUDIT_FILE, audit)


def _append_audit(audit: dict, action: str, **kwargs) -> None:
    entry = {"timestamp": _utc_now_z(), "action": action}
    entry.update(kwargs)
    audit.setdefault("entries", []).append(entry)
    _save_audit(audit)


def _log_audit(action: str, **kwargs) -> None:
    with _audit_lock():
        _append_audit(_load_audit(), action, **kwargs)


def _count_recent(audit: dict, action: str, group_id: str | None = None) -> int:
    now = datetime.now(timezone.utc)
    count = 0
    for entry in audit.get("entries", []):
        if not isinstance(entry, dict) or entry.get("action") != action:
            continue
        if group_id is not None and entry.get("target_group_id") != group_id:
            continue
        try:
            timestamp = datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if 0 <= (now - timestamp).total_seconds() < 3600:
                count += 1
        except (ValueError, KeyError, TypeError):
            continue
    return count


def _rate_limited(group_id: str) -> bool:
    return _count_recent(_load_audit(), "triage_post", group_id) >= MAX_POSTS_PER_HOUR_PER_GROUP


def _rate_limited_auto() -> bool:
    return _count_recent(_load_audit(), "auto_post") >= MAX_POSTS_PER_HOUR_PER_GROUP


def _cooldown_active(group_id: str, thread_id: str, cooldown_minutes: int) -> bool:
    if cooldown_minutes <= 0:
        return False
    now = datetime.now(timezone.utc)
    for entry in reversed(_load_audit().get("entries", [])):
        if (
            not isinstance(entry, dict)
            or entry.get("action") != "triage_post"
            or entry.get("target_group_id") != group_id
            or entry.get("thread_id") != thread_id
        ):
            continue
        try:
            timestamp = datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return 0 <= (now - timestamp).total_seconds() < cooldown_minutes * 60
        except (ValueError, KeyError, TypeError):
            return False
    return False


def _content_safe(content: str) -> bool:
    # Evaluate across line/control-character boundaries so formatting cannot
    # split an otherwise prohibited instruction into regex-safe fragments.
    normalized = "".join(
        " " if char.isspace() else (
            "" if unicodedata.category(char).startswith("C") else char
        )
        for char in content
    )
    normalized = " ".join(normalized.split())
    return not any(pattern.search(normalized) for pattern in REMEDIATION_PATTERNS)


def _safe_context_link(value: object, allowed_hosts: set[str]) -> str:
    text = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return "No link available"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(char.isspace() for char in text)
        or any(char in text for char in "<>|`")
        or parsed.hostname.casefold() not in allowed_hosts
    ):
        return "No link available"
    return text[:2048]


def generate_draft(item: dict, group_config: dict) -> dict:
    """Generate a non-remediating template draft without invoking an LLM."""
    if not isinstance(item, dict) or not isinstance(group_config, dict):
        return {"draft": "", "blocked": True, "block_reason": "invalid input"}
    triage = group_config.get("triage", {})
    if not isinstance(triage, dict) or triage.get("enabled") is not True:
        return {"draft": "", "blocked": True, "block_reason": "triage not enabled for this group"}
    allowed_types = triage.get("allowed_item_types", [])
    if not isinstance(allowed_types, list) or not all(
        isinstance(value, str) and 0 < len(value) <= 64 for value in allowed_types
    ):
        return {"draft": "", "blocked": True, "block_reason": "invalid allowed item type policy"}
    if allowed_types and item.get("type") not in allowed_types:
        return {"draft": "", "blocked": True,
                "block_reason": f"item type '{item.get('type')}' not in allowed types"}
    keywords = triage.get("trigger_keywords", [])
    if not isinstance(keywords, list) or not all(
        isinstance(value, str) and 0 < len(value) <= 100 for value in keywords
    ):
        return {"draft": "", "blocked": True, "block_reason": "invalid trigger keyword policy"}
    if keywords:
        text = f"{item.get('title', '')} {item.get('preview', '')}".casefold()
        if not any(keyword.casefold() in text for keyword in keywords):
            return {"draft": "", "blocked": True, "block_reason": "no trigger keyword matched"}

    group_id = str(group_config.get("id") or "unknown")
    if _rate_limited(group_id):
        return {"draft": "", "blocked": True, "block_reason": "rate limit reached for this group"}
    cooldown_value = triage.get("cooldown_minutes", 0)
    if (
        isinstance(cooldown_value, bool)
        or not isinstance(cooldown_value, int)
        or not 0 <= cooldown_value <= 10_080
    ):
        return {"draft": "", "blocked": True, "block_reason": "invalid cooldown policy"}
    cooldown = cooldown_value
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    thread_id = str(metadata.get("thread_ts") or metadata.get("ts") or item.get("id") or "")
    if thread_id and _cooldown_active(group_id, thread_id, cooldown):
        return {"draft": "", "blocked": True, "block_reason": "thread cooldown is active"}

    combined = f"{item.get('title', '')} {item.get('preview', '')}".casefold()
    template_key = "crash" if "crash" in combined else ("sev" if "sev" in combined or "incident" in combined else "default")
    configured_hosts = triage.get("allowed_link_hosts", [])
    if isinstance(configured_hosts, str):
        configured_hosts = [configured_hosts]
    allowed_hosts = {
        str(host).strip().casefold()
        for host in configured_hosts[:100]
        if isinstance(host, str)
        and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host.strip())
    } if isinstance(configured_hosts, list) else set()
    if not allowed_hosts:
        allowed_hosts = set(_DEFAULT_LINK_HOSTS.get(str(item.get("source") or ""), set()))
    draft = TEMPLATES[template_key].format(
        prefix=ATTRIBUTION_PREFIX,
        context_links=_safe_context_link(item.get("url"), allowed_hosts),
    )
    if len(draft) > MAX_CONTENT_LENGTH:
        return {"draft": "", "blocked": True, "block_reason": "draft exceeds content limit"}
    return {"draft": draft, "blocked": False, "block_reason": ""}


def _find_pollen(pollen_id: str, source: str = "") -> dict | None:
    hive = load_json(POLLEN_FILE, {"pollen": []}, dict)
    pollen = hive.get("pollen", [])
    if not isinstance(pollen, list):
        raise StateFileError(f"Cannot read {POLLEN_FILE}: pollen must be a list")
    matches = [
        item for item in pollen
        if (
            isinstance(item, dict)
            and item.get("id") == pollen_id
            and (not source or item.get("source") == source)
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _policy_fingerprint(policy: dict) -> str:
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _transport_for(config: dict, target_group: str) -> dict | None:
    autonomy = config.get("autonomy") if isinstance(config.get("autonomy"), dict) else {}
    transports = autonomy.get("transports") if isinstance(autonomy.get("transports"), dict) else {}
    transport = transports.get(target_group)
    return transport if isinstance(transport, dict) else None


def _group_policy(config: dict, alias: str) -> dict | None:
    if not _ALIAS_RE.fullmatch(alias):
        return None
    autonomy = config.get("autonomy") if isinstance(config.get("autonomy"), dict) else {}
    policies = (
        autonomy.get("group_policies")
        if isinstance(autonomy.get("group_policies"), dict)
        else {}
    )
    policy = policies.get(alias)
    if not isinstance(policy, dict):
        return None
    target = str(policy.get("id") or "").strip()
    if not target:
        return None
    return {**policy, "id": target}


def _matching_group_policy(config: dict, item: dict) -> tuple[str, dict] | None:
    """Select one trusted policy from exact source/group match rules."""
    autonomy = config.get("autonomy") if isinstance(config.get("autonomy"), dict) else {}
    policies = autonomy.get("group_policies")
    if not isinstance(policies, dict):
        return None
    matches: list[tuple[str, dict]] = []
    for alias, raw_policy in policies.items():
        if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
            continue
        policy = _group_policy(config, alias)
        if policy is None:
            continue
        sources = policy.get("match_sources", [])
        groups = policy.get("match_groups", [])
        if isinstance(sources, str):
            sources = [sources]
        if isinstance(groups, str):
            groups = [groups]
        if not isinstance(sources, list) or not isinstance(groups, list):
            continue
        if sources and item.get("source") not in sources:
            continue
        if groups and item.get("group") not in groups:
            continue
        # A policy with no match rule must be chosen explicitly in code and is
        # never selected from untrusted scanner fields.
        if not sources and not groups:
            continue
        matches.append((alias, policy))
    return matches[0] if len(matches) == 1 else None


def _ticket_path(ticket_id: str) -> Path | None:
    if not _TICKET_RE.fullmatch(ticket_id):
        return None
    return DRAFTS_DIR / f"{ticket_id}.json"


def _prepare_drafts_dir() -> str | None:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    cutoff = datetime.now(timezone.utc).timestamp() - MAX_TICKET_AGE_MINUTES * 60
    remaining = 0
    try:
        for path in DRAFTS_DIR.glob("*.json"):
            if not path.is_file():
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
            else:
                remaining += 1
    except OSError as exc:
        return f"Cannot maintain draft tickets: {exc}"
    if remaining >= 1000:
        return "Too many active draft tickets"
    return None


def create_draft_ticket(pending_number: int, group_alias: str = "") -> dict:
    """Create a safe local ticket that binds a draft to one exact pollen item."""
    if (
        isinstance(pending_number, bool)
        or not isinstance(pending_number, int)
        or pending_number < 1
    ):
        return {"error": "pending_number must be a positive integer"}
    draft_dir_error = _prepare_drafts_dir()
    if draft_dir_error:
        return {"error": draft_dir_error}
    try:
        config = _load_config()
        hive = load_json(POLLEN_FILE, {"pollen": []}, dict)
    except StateFileError as exc:
        return {"error": str(exc)}
    if not isinstance(hive.get("pollen", []), list):
        return {"error": f"Cannot read {POLLEN_FILE}: pollen must be a list"}
    pending = get_pending(hive)
    if pending_number > len(pending):
        return {"error": f"Pending pollen #{pending_number} does not exist"}
    item = pending[pending_number - 1]
    if group_alias:
        policy = _group_policy(config, group_alias)
        selected = (group_alias, policy) if policy is not None else None
    else:
        selected = _matching_group_policy(config, item)
    if selected is None:
        return {
            "error": "Pollen does not match exactly one configured triage group policy"
        }
    group_alias, policy = selected
    try:
        result = generate_draft(item, policy)
    except StateFileError as exc:
        return {"error": str(exc)}
    if result.get("blocked"):
        return result

    ticket_id = uuid.uuid4().hex
    ticket_path = _ticket_path(ticket_id)
    assert ticket_path is not None
    atomic_write_json(ticket_path, {
        "schema_version": 1,
        "created_at": _utc_now_z(),
        "pollen_id": str(item.get("id") or ""),
        "pollen_source": str(item.get("source") or ""),
        "target_group": str(policy["id"]),
        "group_alias": group_alias,
        "policy_fingerprint": _policy_fingerprint(policy),
        "draft": result["draft"],
    })
    return {
        "ticket_id": ticket_id,
        "pollen_number": pending_number,
        "pollen_id": str(item.get("id") or ""),
        "target_group": str(policy["id"]),
        "draft": result["draft"],
        "expires_in_minutes": MAX_TICKET_AGE_MINUTES,
    }


def post_draft_ticket(ticket_id: str) -> dict:
    """Post the exact fixed-template draft stored in a short-lived ticket."""
    ticket_path = _ticket_path(ticket_id)
    if ticket_path is None or not ticket_path.exists():
        return {"error": "Draft ticket does not exist", "gate": "ticket"}
    try:
        ticket = load_json(ticket_path, {}, dict)
        created = datetime.fromisoformat(
            str(ticket.get("created_at") or "").replace("Z", "+00:00")
        )
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except (StateFileError, TypeError, ValueError) as exc:
        return {"error": f"Invalid draft ticket: {exc}", "gate": "ticket"}
    age = datetime.now(timezone.utc) - created.astimezone(timezone.utc)
    if age.total_seconds() < 0 or age > timedelta(minutes=MAX_TICKET_AGE_MINUTES):
        ticket_path.unlink(missing_ok=True)
        return {"error": "Draft ticket expired", "gate": "ticket"}

    try:
        config = _load_config()
    except StateFileError as exc:
        return {"error": str(exc), "gate": "state"}
    alias = str(ticket.get("group_alias") or "")
    policy = _group_policy(config, alias)
    target_group = str(ticket.get("target_group") or "")
    if (
        policy is None
        or str(policy.get("id") or "") != target_group
        or ticket.get("policy_fingerprint") != _policy_fingerprint(policy)
    ):
        return {"error": "Group policy changed after draft creation", "gate": "allowlist"}
    result = post_triage_response(
        str(ticket.get("pollen_id") or ""),
        target_group,
        str(ticket.get("draft") or ""),
        pollen_source=str(ticket.get("pollen_source") or ""),
    )
    if result.get("status") == "posted":
        ticket_path.unlink(missing_ok=True)
    return result


def _post_slack(
    transport: dict, target_group: str, content: str, client_msg_id: str, thread_ts: str = ""
) -> tuple[bool, str, bool]:
    token_env = str(transport.get("token_env") or "SLACK_TOKEN")
    if not _ENV_RE.fullmatch(token_env):
        return False, "invalid Slack token_env", True
    token = os.environ.get(token_env, "")
    if (
        not token
        or len(token) > 8192
        or any(ord(char) < 32 or ord(char) == 127 for char in token)
    ):
        return False, f"Slack credential {token_env} is not set", True
    channel = str(transport.get("channel_id") or target_group).strip()
    if not _SLACK_CHANNEL_RE.fullmatch(channel):
        return False, "Slack channel_id is missing or invalid", True
    if thread_ts and not _SLACK_TS_RE.fullmatch(thread_ts):
        return False, "Slack thread timestamp is invalid", True
    payload = {
        "channel": channel,
        "text": content,
        "client_msg_id": client_msg_id,
        "mrkdwn": False,
        "link_names": False,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        request = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with _transport_urlopen(request, timeout=15) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                return False, "Slack response exceeded size limit", False
            result = json.loads(
                raw,
                object_pairs_hook=_strict_response_object,
                parse_constant=_reject_response_constant,
            )
            if isinstance(result, dict) and result.get("ok") is True:
                timestamp = result.get("ts", "")
                if (
                    not isinstance(timestamp, str)
                    or not _SLACK_TS_RE.fullmatch(timestamp)
                ):
                    return False, "Slack returned an invalid message timestamp", False
                return True, timestamp, False
            error = result.get("error", "unknown_error") if isinstance(result, dict) else "invalid_response"
            return False, f"Slack rejected the post: {error}", True
    except urllib.error.HTTPError as exc:
        retry_safe = 400 <= exc.code < 500 and exc.code != 408
        return False, f"Slack returned HTTP {exc.code}", retry_safe
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"Slack post failed: {exc}", False


def _send_transport(
    transport: dict,
    target_group: str,
    content: str,
    client_msg_id: str,
    thread_ts: str = "",
) -> tuple[bool, str, bool]:
    transport_type = str(transport.get("type") or "").casefold()
    if transport_type == "slack":
        return _post_slack(transport, target_group, content, client_msg_id, thread_ts)
    return False, f"unsupported or missing transport type '{transport_type}'", True


def _prior_delivery_result(
    audit: dict,
    client_msg_id: str,
    *,
    attempt_action: str,
    success_action: str,
    failure_action: str,
) -> dict | None:
    """Prevent a crash or ambiguous timeout from duplicating an external post."""
    matching = [
        entry
        for entry in audit.get("entries", [])
        if isinstance(entry, dict) and entry.get("client_msg_id") == client_msg_id
    ]
    for entry in reversed(matching):
        if entry.get("action") == success_action:
            return {
                "status": "posted",
                "already_posted": True,
                "remote_id": str(entry.get("remote_id") or ""),
            }
    latest_attempt = next(
        (
            index
            for index in range(len(matching) - 1, -1, -1)
            if matching[index].get("action") == attempt_action
        ),
        None,
    )
    if latest_attempt is None:
        return None
    terminal = next(
        (
            entry
            for entry in matching[latest_attempt + 1 :]
            if entry.get("action") in {success_action, failure_action}
        ),
        None,
    )
    if terminal is not None and terminal.get("action") == failure_action:
        if terminal.get("retry_safe") is True:
            return None
    return {
        "error": "A prior delivery attempt has an unknown outcome; verify remotely before retrying",
        "gate": "idempotency",
    }


def post_triage_response(
    pollen_id: str,
    target_group: str = "",
    draft: str = "",
    *,
    pollen_source: str = "",
) -> dict:
    """Post an explicitly supplied, user-confirmed draft through a real transport."""
    try:
        config = _load_config()
        pollen = _find_pollen(pollen_id, pollen_source)
    except StateFileError as exc:
        return {"error": str(exc), "gate": "state"}
    autonomy = config.get("autonomy") if isinstance(config.get("autonomy"), dict) else {}
    if autonomy.get("enabled") is not True:
        _log_audit("triage_blocked", pollen_id=pollen_id, gate="autonomy")
        return {"error": "Autonomy is disabled", "gate": "autonomy"}
    if pollen is None:
        return {"error": f"Pollen {pollen_id} not found or is ambiguous", "gate": "pollen"}
    if pollen.get("status") != "pending":
        return {"error": f"Pollen {pollen_id} is no longer pending", "gate": "pollen"}
    if not draft:
        return {"error": "A user-confirmed draft must be supplied", "gate": "draft"}
    if not isinstance(draft, str) or len(draft) > MAX_CONTENT_LENGTH:
        return {"error": "Draft exceeds content limit", "gate": "draft"}
    oncall_groups = autonomy.get("oncall_groups", [])
    if not isinstance(oncall_groups, list) or target_group not in oncall_groups:
        _log_audit("triage_blocked", pollen_id=pollen_id, gate="allowlist", target_group_id=target_group)
        return {"error": f"Group '{target_group}' not in oncall_groups allowlist", "gate": "allowlist"}
    if not _content_safe(draft):
        _log_audit("triage_blocked", pollen_id=pollen_id, gate="content")
        return {"error": "Draft contains remediation language", "gate": "content"}
    if not draft.startswith((ATTRIBUTION_PREFIX, REQUIRED_PREFIX)):
        return {"error": "Draft missing required attribution prefix", "gate": "prefix"}
    transport = _transport_for(config, target_group)
    if transport is None:
        return {"error": "No real transport configured for this group", "gate": "transport"}
    metadata = pollen.get("metadata") if isinstance(pollen.get("metadata"), dict) else {}
    thread_id = str(metadata.get("thread_ts") or metadata.get("ts") or pollen_id)
    matching_policies = [
        value
        for value in (
            autonomy.get("group_policies", {}).values()
            if isinstance(autonomy.get("group_policies"), dict)
            else []
        )
        if isinstance(value, dict) and str(value.get("id") or "") == target_group
    ]
    if len(matching_policies) != 1:
        return {"error": "Target group policy is missing or ambiguous", "gate": "policy"}
    policy = matching_policies[0]
    triage_policy = policy.get("triage") if isinstance(policy.get("triage"), dict) else {}
    if triage_policy.get("enabled") is not True:
        return {"error": "Triage is disabled for the target group", "gate": "policy"}
    cooldown_value = triage_policy.get("cooldown_minutes", 0)
    if (
        isinstance(cooldown_value, bool)
        or not isinstance(cooldown_value, int)
        or not 0 <= cooldown_value <= 10_080
    ):
        return {"error": "Group cooldown policy is invalid", "gate": "policy"}
    cooldown = cooldown_value
    resolved_source = str(pollen.get("source") or pollen_source)
    client_msg_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"hivescanner:{resolved_source}:{pollen_id}:{target_group}:{draft}",
        )
    )

    # Hold the audit lock across the final gates, send, and audit append so two
    # local callers cannot both pass the same rate/cooldown checks.
    with _audit_lock():
        audit = _load_audit()
        prior_result = _prior_delivery_result(
            audit,
            client_msg_id,
            attempt_action="triage_attempt",
            success_action="triage_post",
            failure_action="triage_post_failed",
        )
        if prior_result is not None:
            if prior_result.get("status") == "posted":
                return {
                    **prior_result,
                    "pollen_id": pollen_id,
                    "content_length": len(draft),
                }
            return prior_result
        if _count_recent(audit, "triage_post", target_group) >= MAX_POSTS_PER_HOUR_PER_GROUP:
            return {"error": "Rate limit reached for this group", "gate": "rate_limit"}
        if _count_recent(audit, "triage_attempt", target_group) >= MAX_ATTEMPTS_PER_HOUR_PER_GROUP:
            return {"error": "Post-attempt rate limit reached for this group", "gate": "rate_limit"}
        if cooldown and _cooldown_active_from_audit(audit, target_group, thread_id, cooldown):
            return {"error": "Thread cooldown is active", "gate": "cooldown"}
        _append_audit(
            audit,
            "triage_attempt",
            pollen_id=pollen_id,
            target_group_id=target_group,
            client_msg_id=client_msg_id,
        )
        ok, detail, retry_safe = _send_transport(
            transport,
            target_group,
            draft,
            client_msg_id,
            str(metadata.get("thread_ts") or metadata.get("ts") or "")
            if pollen.get("source") == "slack"
            else "",
        )
        if not ok:
            _append_audit(audit, "triage_post_failed", pollen_id=pollen_id,
                          target_group_id=target_group, error=detail[:300],
                          client_msg_id=client_msg_id, retry_safe=retry_safe)
            return {"error": detail, "gate": "transport"}
        _append_audit(
            audit,
            "triage_post",
            pollen_id=pollen_id,
            target_group_id=target_group,
            thread_id=thread_id,
            content_length=len(draft),
            transport_type=str(transport.get("type") or ""),
            remote_id=detail,
            client_msg_id=client_msg_id,
        )
    return {"status": "posted", "pollen_id": pollen_id, "content_length": len(draft), "remote_id": detail}


def _cooldown_active_from_audit(
    audit: dict, group_id: str, thread_id: str, cooldown_minutes: int
) -> bool:
    now = datetime.now(timezone.utc)
    for entry in reversed(audit.get("entries", [])):
        if not isinstance(entry, dict):
            continue
        if (
            entry.get("action") == "triage_post"
            and entry.get("target_group_id") == group_id
            and entry.get("thread_id") == thread_id
        ):
            try:
                timestamp = datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                return 0 <= (now - timestamp).total_seconds() < cooldown_minutes * 60
            except (ValueError, KeyError, TypeError):
                return False
    return False


def post_auto_response(target_id: str, content: str) -> dict:
    """Post an automated message through the same safety and transport gates."""
    try:
        config = _load_config()
    except StateFileError as exc:
        return {"error": str(exc)}
    autonomy = config.get("autonomy") if isinstance(config.get("autonomy"), dict) else {}
    if autonomy.get("enabled") is not True:
        return {"error": "Autonomy is disabled"}
    if target_id not in autonomy.get("oncall_groups", []):
        return {"error": "Target is not allowlisted"}
    if not isinstance(content, str):
        return {"error": "Content must be text"}
    if not content.startswith(AUTO_POST_PREFIX):
        content = f"{AUTO_POST_PREFIX}\n\n{content}"
    if len(content) > MAX_CONTENT_LENGTH or not _content_safe(content):
        return {"error": "Content failed safety or length checks"}
    transport = _transport_for(config, target_id)
    if transport is None:
        return {"error": "No real transport configured for this target"}
    client_msg_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hivescanner:auto:{target_id}:{content}"))
    with _audit_lock():
        audit = _load_audit()
        prior_result = _prior_delivery_result(
            audit,
            client_msg_id,
            attempt_action="auto_attempt",
            success_action="auto_post",
            failure_action="auto_post_failed",
        )
        if prior_result is not None:
            if prior_result.get("status") == "posted":
                return {
                    **prior_result,
                    "target_id": target_id,
                    "content_length": len(content),
                }
            return prior_result
        if _count_recent(audit, "auto_post") >= MAX_POSTS_PER_HOUR_PER_GROUP:
            return {"error": "Auto-post rate limit reached"}
        if _count_recent(audit, "auto_attempt") >= MAX_ATTEMPTS_PER_HOUR_PER_GROUP:
            return {"error": "Auto-post attempt rate limit reached"}
        _append_audit(
            audit,
            "auto_attempt",
            target_group_id=target_id,
            client_msg_id=client_msg_id,
        )
        ok, detail, retry_safe = _send_transport(
            transport, target_id, content, client_msg_id
        )
        if not ok:
            _append_audit(
                audit,
                "auto_post_failed",
                target_group_id=target_id,
                error=detail[:300],
                client_msg_id=client_msg_id,
                retry_safe=retry_safe,
            )
            return {"error": detail}
        _append_audit(
            audit,
            "auto_post",
            target_group_id=target_id,
            content_length=len(content),
            remote_id=detail,
            client_msg_id=client_msg_id,
        )
    return {"status": "posted", "target_id": target_id, "content_length": len(content), "remote_id": detail}


def set_autonomy(enabled: bool) -> dict:
    with advisory_lock(CONFIG_LOCK_FILE):
        try:
            config = _load_config()
        except StateFileError as exc:
            return {"error": str(exc)}
        if not isinstance(config.get("autonomy"), dict):
            config["autonomy"] = {}
        config["autonomy"]["enabled"] = enabled
        atomic_write_json(CONFIG_FILE, config)
    _log_audit("autonomy_toggled", enabled=enabled)
    return {"status": "ok", "autonomy_enabled": enabled}


def autonomy_status() -> dict:
    try:
        config = _load_config()
    except StateFileError as exc:
        return {"error": str(exc)}
    autonomy = config.get("autonomy", {})
    if not isinstance(autonomy, dict):
        autonomy = {}
    return {
        "enabled": autonomy.get("enabled", False),
        "oncall_groups": autonomy.get("oncall_groups", []),
        "configured_transports": sorted(autonomy.get("transports", {}))
        if isinstance(autonomy.get("transports"), dict)
        else [],
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: triage_responder.py <command> [args]"}))
        sys.exit(1)
    command = sys.argv[1]
    if command == "create_ticket":
        if len(sys.argv) != 3:
            print(json.dumps({"error": "create_ticket requires <pending_number>"}))
            sys.exit(1)
        try:
            pending_number = int(sys.argv[2])
        except ValueError:
            print(json.dumps({"error": "pending_number must be an integer"}))
            sys.exit(1)
        print(json.dumps(create_draft_ticket(pending_number), indent=2))
    elif command == "post_ticket":
        if len(sys.argv) != 3:
            print(json.dumps({"error": "post_ticket requires <ticket_id>"}))
            sys.exit(1)
        print(json.dumps(post_draft_ticket(sys.argv[2]), indent=2))
    elif command in {"generate_draft", "post_response", "post_auto"}:
        print(json.dumps({
            "error": (
                f"Unsafe argv command '{command}' is disabled; use create_ticket/post_ticket"
            )
        }))
        sys.exit(1)
    elif command == "autonomy_status":
        print(json.dumps(autonomy_status(), indent=2))
    elif command == "autonomy_set":
        if len(sys.argv) < 3 or sys.argv[2] not in {"on", "off"}:
            print(json.dumps({"error": "autonomy_set requires 'on' or 'off'"}))
            sys.exit(1)
        print(json.dumps(set_autonomy(sys.argv[2] == "on"), indent=2))
    else:
        print(json.dumps({"error": f"Unknown command: {command}"}))
        sys.exit(1)
