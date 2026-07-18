# RSS

Monitors public RSS and Atom feeds with no authentication. Feeds are rotated across polls and the first successful read establishes a quiet baseline.

## Pollen Types

| Type | Description |
|------|-------------|
| `rss_item` | A new feed entry appeared after the committed boundary |

```text
/hive hire rss
```

## Configuration

```json
{
  "rss": {
    "enabled": true,
    "feeds": ["https://example.com/feed.xml"],
    "max_items_per_feed": 20,
    "feeds_per_poll": 2
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `feeds` | `[]` | Up to 25 unique public HTTPS feed URLs |
| `max_items_per_feed` | `20` | Entry safety cap for each fetch, from 1 to 100 |
| `feeds_per_poll` | `2` | Feeds rotated through per poll, from 1 to 5 |

The adapter blocks credentials, fragments, loopback/private/link-local targets, DNS rebinding, non-HTTP(S) redirects, compressed response bombs, oversized XML, DTDs, and excessive XML depth. HTTP feeds may be configured but are not confidential or integrity-protected; prefer HTTPS.
