# Hacker News

Uses the public [HN Search API by Algolia](https://hn.algolia.com/api) to find sufficiently popular stories matching configured keywords and comments that mention a username.

## Pollen Types

| Type | Description |
|------|-------------|
| `hn_top_story` | A matching story meets `min_points` |
| `hn_mention` | A comment contains an exact username mention |

No credential is required:

```text
/hive hire hackernews
```

## Configuration

```json
{
  "hackernews": {
    "enabled": true,
    "watch_keywords": ["specific project"],
    "username": "your-hn-name",
    "min_points": 100,
    "max_items": 100,
    "max_pages": 3,
    "keywords_per_poll": 2
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `watch_keywords` | `[]` | Up to 20 bounded search terms; case-equivalent duplicates are folded |
| `username` | `""` | Optional HN username for comment mention searches |
| `min_points` | `100` | Story points threshold, from 0 to 1,000,000,000 |
| `max_items` | `100` | Algolia page size, from 1 to 100 |
| `max_pages` | `3` | Search page cap, from 1 to 5 |
| `keywords_per_poll` | `2` | Keyword searches rotated through per poll, from 1 to 5 |

Algolia is a search index rather than the canonical Firebase item API, so indexing delay and search semantics remain upstream limitations.
