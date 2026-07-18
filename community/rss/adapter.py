"""RSS/Atom scanner with bounded parsing and SSRF-resistant fetching."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime


MAX_FEED_BYTES = 1_000_000
MAX_REDIRECTS = 3
MAX_FETCH_SECONDS = 20
POLL_BUDGET_SECONDS = 45


def _reject_duplicate_key(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _strict_json(raw: str) -> object:
    return json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_key,
        parse_constant=_reject_constant,
    )


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1]


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _public_addresses(hostname: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"DNS lookup failed: {exc}") from exc
    addresses = []
    for info in infos:
        address = str(info[4][0])
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("feed resolves to a non-public address")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError("feed has no addresses")
    return addresses


def _fetch_public_url(url: str) -> tuple[bytes, str]:
    """Fetch while connecting to the already-validated IP (prevents DNS rebinding)."""
    current = url
    deadline = time.monotonic() + MAX_FETCH_SECONDS
    for _ in range(MAX_REDIRECTS + 1):
        if time.monotonic() >= deadline:
            raise TimeoutError("feed fetch time budget exhausted")
        parsed = urllib.parse.urlsplit(current)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("feed URL must be a credential-free HTTP(S) URL")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise ValueError("invalid feed URL port") from exc
        addresses = _public_addresses(parsed.hostname, port)
        last_error: Exception | None = None
        conn: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        for address in addresses:
            sock = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("feed fetch time budget exhausted")
                timeout = min(5.0, max(0.1, remaining))
                sock = socket.create_connection((address, port), timeout=timeout)
                if parsed.scheme == "https":
                    context = ssl.create_default_context()
                    sock = context.wrap_socket(sock, server_hostname=parsed.hostname)
                    conn = http.client.HTTPSConnection(
                        parsed.hostname, port, timeout=timeout, context=context
                    )
                else:
                    conn = http.client.HTTPConnection(
                        parsed.hostname, port, timeout=timeout
                    )
                conn.sock = sock
                break
            except (OSError, ssl.SSLError) as exc:
                last_error = exc
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if conn:
                    conn.close()
                conn = None
        if conn is None:
            raise OSError(f"could not connect to feed: {last_error}")
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        host_header = (
            f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        )
        default_port = 443 if parsed.scheme == "https" else 80
        if port != default_port:
            host_header = f"{host_header}:{port}"
        try:
            conn.request(
                "GET",
                target,
                headers={
                    "Host": host_header,
                    "User-Agent": "HiveScanner/1.0",
                    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
                    "Connection": "close",
                },
            )
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                response.read(MAX_FEED_BYTES + 1)
                if not location:
                    raise ValueError("redirect omitted Location")
                redirected = urllib.parse.urljoin(current, location)
                if (
                    parsed.scheme == "https"
                    and urllib.parse.urlsplit(redirected).scheme != "https"
                ):
                    raise ValueError("HTTPS feed redirected to insecure HTTP")
                current = redirected
                continue
            if response.status != 200:
                raise OSError(f"feed returned HTTP {response.status}")
            content_type = (response.getheader("Content-Type") or "").casefold()
            if content_type and not any(
                value in content_type for value in ("xml", "rss", "atom", "text/plain")
            ):
                raise ValueError(f"unexpected feed content type: {content_type[:80]}")
            body = response.read(MAX_FEED_BYTES + 1)
            if len(body) > MAX_FEED_BYTES:
                raise ValueError("feed exceeded 1 MB")
            return body, current
        finally:
            conn.close()
    raise ValueError("too many feed redirects")


class RssScanner:
    name = "rss"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "feeds": [],
            "max_items_per_feed": 20,
            "feeds_per_poll": 2,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _load_state(watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") in {2, 3, 4}:
            state.setdefault("next_feed_index", 0)
            feeds = state.get("feeds")
            next_index = state["next_feed_index"]
            if (
                not isinstance(feeds, dict)
                or len(feeds) > 25
                or isinstance(next_index, bool)
                or not isinstance(next_index, int)
                or not 0 <= next_index <= 1_000_000
            ):
                return None
            for feed_key, feed_state in feeds.items():
                if (
                    not isinstance(feed_key, str)
                    or re.fullmatch(r"[0-9a-f]{16}", feed_key) is None
                    or not isinstance(feed_state, dict)
                    or not isinstance(feed_state.get("initialized"), bool)
                ):
                    return None
                # Version 2 did not yet have a chronological boundary. It is
                # deliberately re-bootstrapped quietly on the next success.
                if "max_published" not in feed_state:
                    continue
                published = feed_state.get("max_published")
                ids = feed_state.get("ids_at_max_published")
                undated = feed_state.get("undated_seen")
                if (
                    not isinstance(published, str)
                    or (published and _parse_date(published) is None)
                    or not isinstance(ids, list)
                    or len(ids) > 100
                    or not isinstance(undated, list)
                    or len(undated) > 100
                    or not all(
                        isinstance(value, str)
                        and re.fullmatch(r"[0-9a-f]{24}", value)
                        for value in [*ids, *undated]
                    )
                ):
                    return None
            state["version"] = 4
            return state
        if isinstance(watermark, str) and watermark.lstrip().startswith(("{", "[")):
            return None
        return {
            "version": 4,
            "feeds": {},
            "next_feed_index": 0,
            "legacy_watermark": watermark,
        }

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _entry_text(entry: ET.Element, names: list[str], namespace: dict) -> str:
        del namespace
        wanted = {_local_name(name) for name in names}
        for element in entry.iter():
            if element is entry or _local_name(element.tag) not in wanted:
                continue
            text = "".join(element.itertext()).strip()
            if text:
                return text
        return ""

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        poll_deadline = time.monotonic() + POLL_BUDGET_SECONDS
        feeds = config.get("feeds", [])
        if not isinstance(feeds, list):
            return [], watermark
        if len(feeds) > 25 or not all(isinstance(value, str) for value in feeds):
            print("[rss] feeds must contain at most 25 URL strings", file=sys.stderr)
            return [], watermark
        if any(
            not value
            or value != value.strip()
            or len(value) > 2048
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            for value in feeds
        ):
            print("[rss] feed URLs must be bounded and contain no whitespace", file=sys.stderr)
            return [], watermark
        max_items = config.get("max_items_per_feed", 20)
        feeds_per_poll = config.get("feeds_per_poll", 2)
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or not 1 <= max_items <= 100
            or isinstance(feeds_per_poll, bool)
            or not isinstance(feeds_per_poll, int)
            or not 1 <= feeds_per_poll <= 5
        ):
            print("[rss] invalid feed polling limits", file=sys.stderr)
            return [], watermark
        state = self._load_state(watermark)
        if state is None:
            print("[rss] invalid persisted state; preserving watermark", file=sys.stderr)
            return [], watermark
        feed_states = state["feeds"]
        pollen: list[dict] = []
        discovered_at = self._utc_now_z()
        ordered_feeds = list(dict.fromkeys(feeds))
        if ordered_feeds:
            raw_next_index = state.get("next_feed_index", 0)
            if isinstance(raw_next_index, bool) or not isinstance(raw_next_index, int):
                print("[rss] invalid saved feed scheduler", file=sys.stderr)
                return [], watermark
            next_index = raw_next_index % len(ordered_feeds)
            selected_count = min(feeds_per_poll, len(ordered_feeds))
            selected_feeds = [
                ordered_feeds[(next_index + offset) % len(ordered_feeds)]
                for offset in range(selected_count)
            ]
            candidate_next_index = (next_index + selected_count) % len(ordered_feeds)
        else:
            selected_feeds = []
            next_index = 0
            candidate_next_index = 0
        progressed_feeds = 0

        for feed_url in selected_feeds:
            if poll_deadline - time.monotonic() < MAX_FETCH_SECONDS + 2:
                print("[rss] poll time budget nearly exhausted", file=sys.stderr)
                break
            try:
                xml_data, final_url = _fetch_public_url(feed_url)
                lowered_xml = xml_data.lower()
                if b"<!doctype" in lowered_xml or b"<!entity" in lowered_xml:
                    raise ValueError("DTD/entity declarations are not accepted")
                root = ET.fromstring(xml_data)
                element_count = 0
                stack = [(root, 0)]
                while stack:
                    element, depth = stack.pop()
                    element_count += 1
                    if element_count > 20_000 or depth > 100:
                        raise ValueError("feed XML structure exceeded limits")
                    stack.extend((child, depth + 1) for child in list(element))
            except (
                OSError,
                ValueError,
                http.client.HTTPException,
                ET.ParseError,
                RecursionError,
            ) as exc:
                print(f"[rss] feed fetch failed: {exc}", file=sys.stderr)
                continue

            namespace = {"atom": "http://www.w3.org/2005/Atom"}
            entries = [
                element
                for element in root.iter()
                if _local_name(element.tag) in {"item", "entry"}
            ]
            if len(entries) > 10_000:
                print("[rss] feed contained too many entries", file=sys.stderr)
                continue
            feed_key = hashlib.sha256(feed_url.encode()).hexdigest()[:16]
            per_feed = feed_states.get(feed_key, {})
            if not isinstance(per_feed, dict):
                per_feed = {}
            # Version-2 state had only a bounded ID set and could re-emit old
            # entries when a long feed exceeded that set. Bootstrap once into
            # a compact chronological boundary instead.
            initialized = bool(per_feed.get("initialized")) and "max_published" in per_feed
            committed_published = str(per_feed.get("max_published") or "")
            committed_ids = {
                str(value)
                for value in per_feed.get("ids_at_max_published", [])
            } if isinstance(per_feed.get("ids_at_max_published", []), list) else set()
            undated_seen = {
                str(value) for value in per_feed.get("undated_seen", [])
            } if isinstance(per_feed.get("undated_seen", []), list) else set()
            next_published = committed_published
            next_published_ids = set(committed_ids)
            next_undated_seen = set(undated_seen)
            emitted = 0
            feed_pollen: list[dict] = []
            dated_records: list[dict] = []
            undated_records: list[dict] = []
            record_fingerprints: set[str] = set()

            for entry in entries:
                title = self._entry_text(entry, ["title", "atom:title"], namespace)
                link = self._entry_text(entry, ["link"], namespace)
                if not link:
                    for link_element in entry.iter():
                        if _local_name(link_element.tag) != "link":
                            continue
                        relation = link_element.get("rel", "alternate")
                        if relation == "alternate" and link_element.get("href"):
                            link = str(link_element.get("href"))
                            break
                link = urllib.parse.urljoin(final_url, link)
                parsed_link = urllib.parse.urlsplit(link)
                if (
                    parsed_link.scheme not in {"http", "https"}
                    or not parsed_link.hostname
                    or parsed_link.username
                    or parsed_link.password
                ):
                    link = ""
                published = self._entry_text(
                    entry,
                    [
                        "pubDate",
                        "date",
                        "published",
                        "updated",
                        "atom:published",
                        "atom:updated",
                    ],
                    namespace,
                )
                guid = self._entry_text(entry, ["guid", "id", "atom:id"], namespace)
                identity = guid or link or (f"{title}:{published}" if title or published else "")
                if not identity:
                    continue
                fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:24]
                if fingerprint in record_fingerprints:
                    continue
                record_fingerprints.add(fingerprint)
                parsed_date = _parse_date(published)
                if (
                    parsed_date is not None
                    and parsed_date.astimezone(timezone.utc)
                    > datetime.now(timezone.utc) + timedelta(minutes=5)
                ):
                    parsed_date = None
                if parsed_date is not None:
                    published_key = parsed_date.astimezone(timezone.utc).isoformat()
                else:
                    published_key = ""
                record = {
                    "fingerprint": fingerprint,
                    "title": title,
                    "link": link,
                    "published": published,
                    "published_key": published_key,
                }
                (dated_records if published_key else undated_records).append(record)

            if len(undated_records) > 100:
                print(
                    f"[rss] feed {feed_url} has more than 100 entries without dates",
                    file=sys.stderr,
                )
                continue

            # Oldest-first lets a capped poll advance to an exact boundary and
            # collect the rest next time without skipping a middle page.
            dated_records.sort(key=lambda value: value["published_key"])
            for record in [*dated_records, *undated_records]:
                fingerprint = record["fingerprint"]
                published_key = record["published_key"]
                if published_key:
                    is_new = (
                        fingerprint not in undated_seen
                        and (
                            published_key > committed_published
                            or (
                                published_key == committed_published
                                and fingerprint not in committed_ids
                            )
                        )
                    )
                else:
                    is_new = fingerprint not in undated_seen

                if initialized and (not is_new or emitted >= max_items):
                    continue

                if published_key:
                    if published_key > next_published:
                        next_published = published_key
                        next_published_ids = {fingerprint}
                    elif published_key == next_published:
                        next_published_ids.add(fingerprint)
                else:
                    next_undated_seen.add(fingerprint)

                if not initialized:
                    continue
                emitted += 1
                feed_pollen.append({
                    "id": f"rss-{feed_key}-{fingerprint}",
                    "source": "rss",
                    "type": "rss_item",
                    "title": record["title"][:100] or "New feed item",
                    "preview": record["title"][:200],
                    "discovered_at": discovered_at,
                    "author": "",
                    "author_name": "",
                    "group": "RSS",
                    "url": record["link"],
                    "metadata": {
                        "feed_url": feed_url,
                        "published": published_key or record["published"][:100],
                    },
                })

            if len(next_published_ids) > 100 or len(next_undated_seen) > 100:
                print(
                    f"[rss] feed {feed_url} lacks a compact chronological boundary",
                    file=sys.stderr,
                )
                continue
            feed_states[feed_key] = {
                "initialized": True,
                "max_published": next_published,
                "ids_at_max_published": sorted(next_published_ids),
                "undated_seen": sorted(next_undated_seen),
            }
            pollen.extend(feed_pollen)
            progressed_feeds += 1

        if progressed_feeds == 0 and candidate_next_index == next_index:
            return [], watermark
        state["version"] = 4
        state["next_feed_index"] = candidate_next_index
        active_keys = {
            hashlib.sha256(value.strip().encode()).hexdigest()[:16]
            for value in feeds
            if value.strip()
        }
        state["feeds"] = {
            key: value for key, value in feed_states.items() if key in active_keys
        }
        state.pop("legacy_watermark", None)
        return pollen, self._dump_state(state)


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = RssScanner()
    if data.get("command") == "poll":
        poll_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": poll_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
