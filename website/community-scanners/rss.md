# RSS

Monitors RSS and Atom feeds for new entries. No authentication required.

## Pollen Types

| Type | Description |
|------|-------------|
| `rss_item` | A new item appeared in a watched feed |

## Prerequisites

None — RSS feeds are public. No API keys needed.

## Install

```
/hive hire rss
```

## Configuration

```json
{
  "rss": {
    "enabled": true,
    "feeds": [
      "https://blog.example.com/rss",
      "https://news.ycombinator.com/rss"
    ],
    "max_items_per_feed": 5
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `feeds` | `[]` | List of RSS/Atom feed URLs to monitor |
| `max_items_per_feed` | `5` | Max items fetched per feed per poll cycle |

## Tips

- Add your company's engineering blog, release feeds, or status page RSS
- Most blogs and news sites offer RSS — look for the RSS icon or add `/rss` or `/feed` to the URL
- Atom feeds are also supported
