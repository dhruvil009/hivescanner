"""Package Tracking scanner — monitors Gmail for shipping updates and delivery notifications."""

from __future__ import annotations

import base64
import binascii
import html
import json
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
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
            or (source.hostname or "").casefold() != (target.hostname or "").casefold()
            or source_port != target_port
        ):
            raise urllib.error.HTTPError(
                newurl, code, "cross-origin redirect refused", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_SameOriginRedirectHandler()).open(
        req, timeout=timeout
    )


def _reject_duplicate_key(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _strict_json(raw: bytes | str) -> object:
    return json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_key,
        parse_constant=_reject_constant,
    )


def _bounded_text(value: object, maximum: int, *, allow_empty: bool = True) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= maximum
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _valid_payload_tree(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    stack: list[tuple[dict, int]] = [(payload, 0)]
    visited = 0
    while stack:
        part, depth = stack.pop()
        visited += 1
        if visited > 2_000 or depth > 20:
            return False
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        children = part.get("parts", [])
        if (
            not _bounded_text(mime, 1_000)
            or not isinstance(body, dict)
            or not isinstance(children, list)
            or len(children) > 1_000
            or not all(isinstance(child, dict) for child in children)
        ):
            return False
        data = body.get("data")
        if data is not None and (
            not isinstance(data, str)
            or len(data) > 4_000_000
            or re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", data) is None
        ):
            return False
        stack.extend((child, depth + 1) for child in children)
    return True


class PackageTrackingScanner:
    name = "package-tracking"
    _POLL_BUDGET_SECONDS = 45

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "GOOGLE_ACCESS_TOKEN",
            "max_items": 20,
            "max_pages": 5,
            "overlap_seconds": 300,
            "search_query": "subject:(shipped OR tracking OR delivery OR out for delivery)",
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _gmail_api(self, path: str, token: str) -> Optional[dict]:
        """Call the Gmail REST API with Bearer token auth."""
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        deadline = getattr(self, "_poll_deadline", None)
        timeout = 15.0
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            timeout = min(timeout, max(0.1, remaining))
        try:
            with _urlopen(req, timeout=timeout) as resp:
                raw = resp.read(3_000_001)
                if len(raw) > 3_000_000:
                    raise ValueError("response exceeded 3 MB")
                result = _strict_json(raw)
                return result if isinstance(result, dict) else None
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as e:
            print(f"[package-tracking] API error ({path}): {e}", file=sys.stderr)
            return None

    def _extract_tracking_number(self, text: str) -> tuple[str, str]:
        """Extract tracking number and carrier from text. Returns (tracking_number, carrier)."""
        # UPS: 1Z followed by 16 alphanumeric characters
        ups_match = re.search(r"\b1Z[A-Z0-9]{16}\b", text)
        if ups_match:
            return ups_match.group(0), "UPS"

        # USPS: starts with 94, 93, 92, or 95 followed by 20-22 digits
        usps_match = re.search(r"\b(94|93|92|95)\d{20,22}\b", text)
        if usps_match:
            return usps_match.group(0), "USPS"

        # FedEx: 12-22 digit number (broad, but only used in shipping email context)
        fedex_match = re.search(r"\b\d{12,22}\b", text) if "fedex" in text.casefold() else None
        if fedex_match:
            return fedex_match.group(0), "FedEx"

        return "", ""

    def _detect_event_type(self, text: str) -> str:
        """Detect the shipping event type from email text."""
        lower = text.lower()
        negative_out_for_delivery = re.search(
            r"\b(?:not|not yet|isn't|is not|wasn't|was not)\s+out for delivery\b",
            lower,
        )
        future_out_for_delivery = re.search(
            r"\b(?:will|may|might|should|expected|scheduled|due|set)\b.{0,40}"
            r"\bout for delivery\b",
            lower,
        )
        if (
            "out for delivery" in lower
            and not negative_out_for_delivery
            and not future_out_for_delivery
        ):
            return "package_out_for_delivery"
        delivery_failed = re.search(
            r"\b(?:not|not yet|wasn't|was not|couldn't|could not|unable to|failed to)\s+"
            r"(?:be\s+|get\s+)?delivered\b|\b(?:delivery failed|undeliverable)\b",
            lower,
        )
        handed_to_carrier = re.search(
            r"\bdelivered\s+to\s+(?:the\s+)?(?:carrier|postal service|courier)\b",
            lower,
        )
        future_delivery = re.search(
            r"\b(?:will|may|might|should|can|expected|scheduled|due|estimated|set)\b"
            r".{0,40}\b(?:be\s+)?delivered\b|\bdelivery\s+(?:is\s+)?"
            r"(?:expected|scheduled|due|estimated)\b",
            lower,
        )
        if (
            re.search(r"\bdelivered\b", lower)
            and not delivery_failed
            and not handed_to_carrier
            and not future_delivery
        ):
            return "package_delivered"
        shipping_failed = re.search(
            r"\b(?:not|not yet|hasn't|has not|wasn't|was not|failed to)\s+(?:been\s+)?shipped\b",
            lower,
        )
        future_shipping = re.search(
            r"\b(?:will|may|might|should|expected|scheduled|due|set|ready)\b"
            r".{0,40}\b(?:be\s+)?shipped\b",
            lower,
        )
        if (
            ("shipped" in lower or "has shipped" in lower)
            and not shipping_failed
            and not future_shipping
        ):
            return "package_shipped"
        return "package_update"

    def _get_header(self, headers: list[dict], name: str) -> str:
        """Extract a header value from Gmail message headers."""
        for h in headers:
            if h["name"].casefold() == name.casefold():
                return h["value"]
        return ""

    @staticmethod
    def _decode_part(data: object) -> str:
        if not isinstance(data, str) or len(data) > 4_000_000:
            return ""
        try:
            padding = "=" * (-len(data) % 4)
            return base64.urlsafe_b64decode(data + padding).decode(
                "utf-8", errors="replace"
            )[:200_000]
        except (binascii.Error, ValueError, UnicodeError):
            return ""

    def _decode_body(self, payload: dict) -> str:
        """Decode a bounded MIME tree, preferring text/plain over HTML."""
        if not isinstance(payload, dict):
            return ""
        stack: list[tuple[dict, int]] = [(payload, 0)]
        visited = 0
        html_fallback = ""
        while stack and visited < 2000:
            part, depth = stack.pop()
            visited += 1
            mime = str(part.get("mimeType") or "").casefold()
            body = part.get("body") if isinstance(part.get("body"), dict) else {}
            decoded = self._decode_part(body.get("data"))
            if decoded and (mime == "text/plain" or (not mime and not part.get("parts"))):
                return decoded
            if decoded and mime == "text/html" and not html_fallback:
                without_active = re.sub(
                    r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)\s*>",
                    " ",
                    decoded,
                )
                html_fallback = html.unescape(
                    re.sub(r"(?s)<[^>]*>", " ", without_active)
                )[:200_000]
            if depth >= 20:
                continue
            children = part.get("parts") if isinstance(part.get("parts"), list) else []
            for child in reversed(children[:1000]):
                if isinstance(child, dict):
                    stack.append((child, depth + 1))
        return " ".join(html_fallback.split())

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        token_env = config.get("token_env", "GOOGLE_ACCESS_TOKEN")
        if not isinstance(token_env, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", token_env
        ):
            return [], watermark
        token = os.environ.get(token_env, "")
        if (
            not token
            or len(token) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in token)
        ):
            return [], watermark

        max_items = config.get("max_items", 20)
        max_pages = config.get("max_pages", 5)
        overlap_seconds = config.get("overlap_seconds", 300)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (max_items, max_pages, overlap_seconds)
            )
            or not 1 <= max_items <= 100
            or not 1 <= max_pages <= 10
            or not 60 <= overlap_seconds <= 3600
        ):
            print("[package-tracking] pagination limits are invalid", file=sys.stderr)
            return [], watermark
        search_query = config.get(
            "search_query",
            "subject:(shipped OR tracking OR delivery OR out for delivery)",
        )
        if (
            not isinstance(search_query, str)
            or not search_query
            or search_query != search_query.strip()
            or len(search_query) > 5000
            or any(ord(char) < 32 or ord(char) == 127 for char in search_query)
        ):
            return [], watermark

        scope = hashlib.sha256(search_query.encode()).hexdigest()[:16]
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") == 3:
            raw_committed_ms = state.get("internal_date_ms")
            raw_seen_ids = state.get("seen_ids")
            if (
                not isinstance(state.get("initialized"), bool)
                or not isinstance(state.get("scope"), str)
                or re.fullmatch(r"[0-9a-f]{16}", state["scope"]) is None
                or isinstance(raw_committed_ms, bool)
                or not isinstance(raw_committed_ms, int)
                or not 0 <= raw_committed_ms <= 32_503_680_000_000
                or not isinstance(raw_seen_ids, list)
                or len(raw_seen_ids) > 6_000
                or not all(_bounded_text(value, 256, allow_empty=False) for value in raw_seen_ids)
                or len(set(raw_seen_ids)) != len(raw_seen_ids)
            ):
                print("[package-tracking] invalid persisted state", file=sys.stderr)
                return [], watermark
            same_scope = state.get("scope") == scope
            initialized = state["initialized"] and same_scope
            committed_ms = raw_committed_ms
            seen_order = (
                list(raw_seen_ids)
                if same_scope
                else []
            )
            seen_ids = set(seen_order)
        elif isinstance(state, dict) and state.get("version") == 2:
            raw_committed_ms = state.get("internal_date_ms")
            if (
                not isinstance(state.get("initialized"), bool)
                or isinstance(raw_committed_ms, bool)
                or not isinstance(raw_committed_ms, int)
                or not 0 <= raw_committed_ms <= 32_503_680_000_000
            ):
                return [], watermark
            initialized = state["initialized"]
            committed_ms = raw_committed_ms
            seen_order = []
            seen_ids = set()
        elif isinstance(watermark, str) and watermark.lstrip().startswith(("{", "[")):
            print("[package-tracking] invalid persisted state", file=sys.stderr)
            return [], watermark
        else:
            initialized = False
            committed_ms = 0
            seen_order = []
            seen_ids = set()
        scan_started_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        effective_query = search_query
        if initialized and committed_ms:
            effective_query = (
                f"({search_query}) after:"
                f"{max(0, committed_ms // 1000 - overlap_seconds)}"
            )

        # Search Gmail for shipping-related emails
        messages = []
        page_token = ""
        seen_page_tokens: set[str] = set()
        pages_to_fetch = max_pages if initialized else 1
        for page in range(pages_to_fetch):
            params = {"q": effective_query, "maxResults": "500"}
            if page_token:
                params["pageToken"] = page_token
            search_result = self._gmail_api(f"messages?{urllib.parse.urlencode(params)}", token)
            if search_result is None or not isinstance(search_result.get("messages", []), list):
                return [], watermark
            page_messages = search_result.get("messages", [])
            if not all(
                isinstance(value, dict)
                and isinstance(value.get("id"), str)
                and value.get("id")
                and len(value["id"]) <= 256
                for value in page_messages
            ):
                return [], watermark
            messages.extend(page_messages)
            raw_page_token = search_result.get("nextPageToken")
            if raw_page_token in (None, ""):
                page_token = ""
            elif (
                not isinstance(raw_page_token, str)
                or len(raw_page_token) > 2000
                or any(ord(char) < 32 or ord(char) == 127 for char in raw_page_token)
            ):
                return [], watermark
            else:
                page_token = raw_page_token
            if page_token and page_token in seen_page_tokens:
                return [], watermark
            if page_token:
                seen_page_tokens.add(page_token)
            if not page_token:
                break
            if page + 1 >= pages_to_fetch:
                if initialized:
                    print("[package-tracking] Gmail backlog exceeded max_pages", file=sys.stderr)
                    return [], watermark
                break
        if not messages:
            next_state = {
                "version": 3,
                "initialized": True,
                "scope": scope,
                "internal_date_ms": scan_started_ms,
                "seen_ids": seen_order[-6000:],
            }
            return [], json.dumps(next_state, sort_keys=True, separators=(",", ":"))

        # First enable intentionally ignores existing matching mail. No full
        # message fetches are needed to establish that quiet boundary.
        if not initialized:
            next_state = {
                "version": 3,
                "initialized": True,
                "scope": scope,
                "internal_date_ms": scan_started_ms,
                "seen_ids": [],
            }
            return [], json.dumps(next_state, sort_keys=True, separators=(",", ":"))

        unprocessed_ids: list[str] = []
        queued_ids: set[str] = set()
        for msg_stub in reversed(messages):
            msg_id = str(msg_stub.get("id") or "")
            if (
                msg_id
                and len(msg_id) <= 256
                and msg_id not in seen_ids
                and msg_id not in queued_ids
            ):
                unprocessed_ids.append(msg_id)
                queued_ids.add(msg_id)
        selected_ids = unprocessed_ids[:max_items]
        fully_drained = len(selected_ids) == len(unprocessed_ids)

        pollen = []
        had_errors = False
        successful_details = 0
        next_seen_order = list(seen_order)

        # Gmail returns newest first. Processing oldest-to-newest makes the
        # bounded ID cache retain the newest opaque IDs.
        for msg_id in selected_ids:

            # Fetch full message
            msg = self._gmail_api(f"messages/{msg_id}?format=full", token)
            if (
                not isinstance(msg, dict)
                or msg.get("id") != msg_id
                or not isinstance(msg.get("payload"), dict)
            ):
                had_errors = True
                continue

            payload = msg["payload"]
            headers = payload.get("headers", [])
            raw_internal_date = msg.get("internalDate")
            if (
                not _valid_payload_tree(payload)
                or not isinstance(headers, list)
                or len(headers) > 10_000
                or not all(
                    isinstance(value, dict)
                    and _bounded_text(value.get("name"), 1_000, allow_empty=False)
                    and isinstance(value.get("value"), str)
                    and len(value["value"]) <= 1_000_000
                    for value in headers
                )
                or not isinstance(raw_internal_date, str)
                or not raw_internal_date.isascii()
                or not raw_internal_date.isdigit()
                or not 1 <= len(raw_internal_date) <= 20
            ):
                had_errors = True
                continue

            subject = self._get_header(headers, "Subject")
            sender = self._get_header(headers, "From")
            internal_date = raw_internal_date
            next_seen_order.append(msg_id)

            # Decode body for tracking number extraction
            body = self._decode_body(payload)
            full_text = f"{subject} {body}"

            # Detect event type from subject and body
            event_type = self._detect_event_type(full_text)

            # Extract tracking number and carrier
            tracking_number, carrier = self._extract_tracking_number(full_text)

            # Extract retailer name from sender (e.g. "Amazon.com <ship@amazon.com>" -> "Amazon.com")
            retailer = sender.split("<")[0].strip().strip('"') if sender else ""
            if not retailer:
                retailer = sender

            # Build title
            event_labels = {
                "package_shipped": "Package shipped",
                "package_out_for_delivery": "Out for delivery",
                "package_delivered": "Package delivered",
                "package_update": "Package update",
            }
            label = event_labels.get(event_type, "Package update")
            title_source = carrier if carrier else retailer
            title = f"{label}: {title_source}"[:100]

            pollen.append({
                "id": f"package-{msg_id}",
                "source": "package-tracking",
                "type": event_type,
                "title": title,
                "preview": subject[:200],
                "discovered_at": self._utc_now_z(),
                "author": sender,
                "author_name": retailer,
                "group": "Packages",
                "url": f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
                "metadata": {
                    "tracking_number": tracking_number,
                    "carrier": carrier,
                    "retailer": retailer,
                    "internal_date_ms": str(internal_date),
                },
            })
            successful_details += 1

        if had_errors and successful_details == 0:
            return [], watermark
        next_state = {
            "version": 3,
            "initialized": True,
            "scope": scope,
            "internal_date_ms": (
                scan_started_ms if fully_drained and not had_errors else committed_ms
            ),
            "seen_ids": next_seen_order[-6000:],
        }
        return pollen, json.dumps(next_state, sort_keys=True, separators=(",", ":"))


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = PackageTrackingScanner()
    if data.get("command") == "poll":
        result_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
