# Facebook

Polls Facebook Page Messenger conversations for incoming messages. This adapter does not read the retired Page notifications feed.

## Pollen Types

| Type | Description |
|------|-------------|
| `facebook_message` | A newly observed incoming message in a watched Page conversation |

## Setup

Create an appropriate Meta app, connect the Messenger product to the Pages you operate, obtain a Page access token with the permissions Meta currently requires for conversation/message access, and export it:

```bash
export FACEBOOK_PAGE_TOKEN="your-page-access-token"
/hive hire facebook
```

Meta app review, Page roles, token lifetime, permissions, and supported fields vary by app and API version; follow the current [Messenger Platform](https://developers.facebook.com/docs/messenger-platform/) and [Graph API](https://developers.facebook.com/docs/graph-api/) documentation.

## Configuration

```json
{
  "facebook": {
    "enabled": true,
    "token_env": "FACEBOOK_PAGE_TOKEN",
    "api_version": "v25.0",
    "watch_pages": ["numeric-page-id"],
    "max_items": 100,
    "max_pages": 3,
    "pages_per_poll": 2,
    "conversations_per_page": 4
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `token_env` | `FACEBOOK_PAGE_TOKEN` | Page-token environment variable |
| `api_version` | `v25.0` | Explicit Graph API version |
| `watch_pages` | `[]` | Up to ten unique numeric Page IDs |
| `max_items` | `100` | Conversation/message page size, from 1 to 100 |
| `max_pages` | `3` | Per-resource page cap, from 1 to 5 |
| `pages_per_poll` | `2` | Pages rotated through per poll, from 1 to 10 |
| `conversations_per_page` | `4` | Conversations processed for each selected Page, from 1 to 20 |

Polling is deliberately bounded. Use Meta webhooks for high-volume or low-latency Messenger workloads.
