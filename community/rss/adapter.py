"""RSS scanner — minimal example community scanner for HiveScanner."""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def _parse_date(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        pass
    try:
        iso = s.rstrip()
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


class RssScanner:
    name = "rss"

    def configure(self) -> dict:
        return {
            "enabled": False,
            "feeds": [],
            "max_items_per_feed": 5,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        feeds = config.get("feeds", [])
        max_items = config.get("max_items_per_feed", 5)
        pollen = []
        had_errors = False

        for feed_url in feeds:
            try:
                req = urllib.request.Request(feed_url, headers={"User-Agent": "HiveScanner/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    xml_data = resp.read()
                root = ET.fromstring(xml_data)

                # Support both RSS and Atom
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                entries = root.findall(".//item") or root.findall(".//atom:entry", ns)

                for entry in entries[:max_items]:
                    title = (entry.findtext("title") or
                             entry.findtext("atom:title", namespaces=ns) or "")
                    link = (entry.findtext("link") or "")
                    if not link:
                        link_el = entry.find("atom:link", ns)
                        if link_el is not None:
                            link = link_el.get("href", "")

                    pub_date = (entry.findtext("pubDate") or
                                entry.findtext("atom:published", namespaces=ns) or "")

                    item_dt = _parse_date(pub_date)
                    wm_dt = _parse_date(watermark)
                    if item_dt and wm_dt and item_dt <= wm_dt:
                        continue

                    pollen_id = f"rss-{hashlib.sha256(f'{feed_url}:{title}'.encode()).hexdigest()[:12]}"
                    pollen.append({
                        "id": pollen_id,
                        "source": "rss",
                        "type": "rss_item",
                        "title": title[:100],
                        "preview": title[:200],
                        "discovered_at": self._utc_now_z(),
                        "author": "",
                        "author_name": "",
                        "group": "RSS",
                        "url": link,
                        "metadata": {"feed_url": feed_url},
                    })
            except Exception as e:
                print(f"[rss] Error fetching {feed_url}: {e}", file=sys.stderr)
                had_errors = True

        if had_errors:
            return pollen, watermark
        return pollen, self._utc_now_z()


# Sandboxed execution support
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = RssScanner()
    if data["command"] == "poll":
        poll_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": poll_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
