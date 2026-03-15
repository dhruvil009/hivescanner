# Notion

Monitors Notion for page updates and new comments.

## Pollen Types

| Type | Description |
|------|-------------|
| `notion_page_updated` | A watched page was updated |
| `notion_comment` | A new comment was added to a watched page |

## Getting a Token

1. Go to [My Integrations](https://www.notion.so/my-integrations)
2. Click **New integration**
3. Name it "HiveScanner" and select your workspace
4. Under **Capabilities**, ensure **Read content** is enabled
5. Copy the **Internal Integration Secret**
6. Add to your shell profile:

```bash
export NOTION_TOKEN="ntn_xxxxxxxxxxxx"
```

7. **Share pages with the integration**: In Notion, open each page/database you want to monitor, click the `...` menu, go to **Connections**, and add "HiveScanner"

::: tip
The integration can only access pages explicitly shared with it. You must add the connection to each page or database you want to monitor.
:::

## Install

```
/hive hire notion
```

## Configuration

```json
{
  "notion": {
    "enabled": true,
    "token_env": "NOTION_TOKEN",
    "watch_databases": ["database-id-1"],
    "watch_pages": ["page-id-1"],
    "max_items": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `token_env` | `NOTION_TOKEN` | Environment variable containing your integration token |
| `watch_databases` | `[]` | Database IDs to monitor for changes |
| `watch_pages` | `[]` | Page IDs to monitor for updates and comments |
| `max_items` | `20` | Max items fetched per poll cycle |

To find a page or database ID: open it in Notion, copy the URL — the ID is the 32-character string after the workspace name.

## API Details

Uses the [Notion API v1](https://developers.notion.com/).
