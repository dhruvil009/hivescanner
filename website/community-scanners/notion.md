# Notion

Monitors explicitly shared Notion pages and data sources for page changes and, optionally, comments.

## Pollen Types

| Type | Description |
|------|-------------|
| `notion_page_updated` | A watched or queried page changed |
| `notion_comment` | A new comment appeared on a tracked page |

## Setup

Create an integration in [Notion My Integrations](https://www.notion.so/my-integrations), enable read access, share each relevant page/database with it, and export the secret:

```bash
export NOTION_TOKEN="ntn_..."
/hive hire notion
```

## Configuration

```json
{
  "notion": {
    "enabled": true,
    "token_env": "NOTION_TOKEN",
    "watch_data_sources": ["data-source-id"],
    "watch_databases": [],
    "watch_pages": ["page-id"],
    "watch_comments": false,
    "integration_user_id": "",
    "max_items": 100,
    "max_pages": 3
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `token_env` | `NOTION_TOKEN` | Integration-token environment variable |
| `watch_data_sources` | `[]` | Data source IDs queried directly |
| `watch_databases` | `[]` | Legacy database IDs resolved to their data sources |
| `watch_pages` | `[]` | Explicit page IDs |
| `watch_comments` | `false` | Poll comments on tracked pages |
| `integration_user_id` | `""` | Optional integration user ID whose own comments are ignored |
| `max_items` | `100` | Page size, from 1 to 100 |
| `max_pages` | `3` | Per-resource page cap, from 1 to 5 |

The three watch lists accept at most five IDs in total. The adapter paces requests for Notion's documented average integration rate and rejects incomplete pagination. For high-volume comments, use [Notion webhooks](https://developers.notion.com/reference/webhooks).
