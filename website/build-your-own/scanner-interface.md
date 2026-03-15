# Scanner Interface

A community scanner is a Python class with two methods. Here's everything you need.

## The Interface

```python
class YourScanner:
    name = "your-scanner"  # unique identifier

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        """Fetch new items since `watermark`. Return (pollen_list, new_watermark)."""
        ...

    def configure(self) -> dict:
        """Return default config template for this scanner."""
        ...
```

- `poll()` fetches new data and returns a list of pollen dicts plus an updated watermark (an ISO timestamp string)
- `configure()` returns sensible defaults for first-time setup

## Pollen Dict (Required Fields)

Every pollen dict must include all of these fields:

```python
{
    "id": "your-scanner-unique-id-12345",
    "source": "your-scanner",
    "type": "item_type",
    "title": "Short title (max 100 chars)",
    "preview": "Longer preview text (max 200 chars)",
    "discovered_at": "2025-01-15T10:30:00Z",
    "author": "username",
    "author_name": "Display Name",
    "group": "Grouping Label",
    "url": "https://link-to-original",
    "metadata": {}
}
```

| Field | Description |
|-------|-------------|
| `id` | Unique identifier for deduplication. Use a prefix like `your-scanner-` |
| `source` | Must match your scanner's `name` |
| `type` | Category of notification (e.g., `issue_assigned`, `new_comment`) |
| `title` | Short title shown in the notification (max 100 chars) |
| `preview` | Longer preview text (max 200 chars) |
| `discovered_at` | ISO 8601 timestamp when the item was discovered |
| `author` | Username of the author |
| `author_name` | Display name of the author |
| `group` | Grouping label (used for batch grouping) |
| `url` | Link to the original item |
| `metadata` | Additional data (free-form dict) |

## Minimal Example

A complete RSS feed scanner in ~30 lines:

```python
"""RSS scanner — minimal community scanner example."""

import hashlib
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


class RssScanner:
    name = "rss"

    def configure(self) -> dict:
        return {"enabled": False, "feeds": [], "max_items_per_feed": 5}

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pollen = []

        for feed_url in config.get("feeds", []):
            req = urllib.request.Request(
                feed_url, headers={"User-Agent": "HiveScanner/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                root = ET.fromstring(resp.read())

            for item in root.findall(".//item")[
                : config.get("max_items_per_feed", 5)
            ]:
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                pub_date = item.findtext("pubDate") or ""

                if pub_date and pub_date <= watermark:
                    continue

                pollen.append({
                    "id": f"rss-{hashlib.sha256(f'{feed_url}:{title}'.encode()).hexdigest()[:12]}",
                    "source": "rss",
                    "type": "rss_item",
                    "title": title[:100],
                    "preview": title[:200],
                    "discovered_at": now,
                    "author": "",
                    "author_name": "",
                    "group": "RSS",
                    "url": link,
                    "metadata": {"feed_url": feed_url},
                })

        return pollen, now


# Required: sandboxed execution entry point
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = RssScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
```
