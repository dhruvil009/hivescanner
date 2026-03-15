"""Package Tracking scanner — monitors Gmail for shipping updates and delivery notifications."""

import json
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional


class PackageTrackingScanner:
    name = "package-tracking"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "token_env": "GOOGLE_ACCESS_TOKEN",
            "max_items": 10,
            "search_query": "subject:(shipped OR tracking OR delivery OR out for delivery) newer_than:1d",
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
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
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
        fedex_match = re.search(r"\b\d{12,22}\b", text)
        if fedex_match:
            return fedex_match.group(0), "FedEx"

        return "", ""

    def _detect_event_type(self, text: str) -> str:
        """Detect the shipping event type from email text."""
        lower = text.lower()
        if "out for delivery" in lower:
            return "package_out_for_delivery"
        if "delivered" in lower or "has been delivered" in lower:
            return "package_delivered"
        if "shipped" in lower or "has shipped" in lower:
            return "package_shipped"
        return "package_shipped"

    def _get_header(self, headers: list[dict], name: str) -> str:
        """Extract a header value from Gmail message headers."""
        for h in headers:
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    def _decode_body(self, payload: dict) -> str:
        """Decode the email body from a Gmail message payload."""
        import base64

        # Try top-level body first
        body_data = payload.get("body", {}).get("data", "")
        if body_data:
            try:
                return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
            except Exception:
                return ""

        # Try multipart parts
        for part in payload.get("parts", []):
            mime = part.get("mimeType", "")
            if mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    try:
                        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    except Exception:
                        continue
            # Recurse into nested multipart
            if part.get("parts"):
                result = self._decode_body(part)
                if result:
                    return result

        return ""

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        token = os.environ.get(config.get("token_env", "GOOGLE_ACCESS_TOKEN"), "")
        if not token:
            return [], watermark

        max_items = config.get("max_items", 10)
        search_query = config.get(
            "search_query",
            "subject:(shipped OR tracking OR delivery OR out for delivery) newer_than:1d",
        )

        # Search Gmail for shipping-related emails
        encoded_query = urllib.parse.quote(search_query)
        search_result = self._gmail_api(
            f"messages?q={encoded_query}&maxResults={max_items}", token
        )
        if not search_result:
            return [], watermark

        messages = search_result.get("messages", [])
        if not messages:
            return [], watermark

        pollen = []

        for msg_stub in messages:
            msg_id = msg_stub.get("id", "")
            if not msg_id:
                continue

            # Fetch full message
            msg = self._gmail_api(f"messages/{msg_id}?format=full", token)
            if not msg:
                continue

            payload = msg.get("payload", {})
            headers = payload.get("headers", [])

            subject = self._get_header(headers, "Subject")
            sender = self._get_header(headers, "From")
            internal_date = msg.get("internalDate", "")

            # Skip messages older than watermark
            if watermark and internal_date:
                try:
                    msg_dt = datetime.fromtimestamp(
                        int(internal_date) / 1000, tz=timezone.utc
                    )
                    wm_dt = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
                    if msg_dt <= wm_dt:
                        continue
                except (ValueError, TypeError, OSError):
                    pass

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
                "url": "",
                "metadata": {
                    "tracking_number": tracking_number,
                    "carrier": carrier,
                    "retailer": retailer,
                },
            })

        return pollen, self._utc_now_z()


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = PackageTrackingScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
