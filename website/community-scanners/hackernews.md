# Hacker News

Monitors Hacker News for top stories matching your keywords and mentions of your username.

## Pollen Types

| Type | Description |
|------|-------------|
| `hn_top_story` | A top story matches one of your keywords |
| `hn_mention` | Your username was mentioned in a comment |

## Prerequisites

None — the Hacker News API is public. No API keys needed.

## Install

```
/hive hire hackernews
```

## Configuration

```json
{
  "hackernews": {
    "enabled": true,
    "watch_keywords": ["rust", "claude code", "your-project-name"],
    "username": "your-hn-username",
    "min_points": 100,
    "max_items": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `watch_keywords` | `[]` | Keywords to match against top stories |
| `username` | `""` | Your HN username (for mention tracking) |
| `min_points` | `100` | Minimum points threshold for keyword matches |
| `max_items` | `20` | Max items fetched per poll cycle |

## Tips

- Use specific keywords to avoid noise — "Claude Code" is better than "AI"
- Set `min_points` higher to only see stories that gain traction
- Leave `username` empty if you only want keyword monitoring

## API Details

Uses the [Hacker News Algolia API](https://hn.algolia.com/api) (public, no authentication).
