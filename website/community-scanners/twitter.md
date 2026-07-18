# Twitter / X

Polls X API v2 for mentions and, when separately authorized, direct-message events.

## Pollen Types

| Type | Description |
|------|-------------|
| `twitter_mention` | A newly observed mention |
| `twitter_dm` | A newly observed incoming DM event |

## Setup

Create an app in the [X Developer Portal](https://developer.x.com/en/portal/dashboard). Mentions use an app bearer token; DMs require a user-context OAuth 2.0 token with the access/scopes X currently requires.

```bash
export TWITTER_BEARER_TOKEN="your-app-bearer-token"
export TWITTER_USER_TOKEN="your-user-context-token"
/hive hire twitter
```

::: warning
Endpoints, retention, quotas, and access tiers are controlled by X and can change. Confirm your current plan grants both endpoints before enabling them.
:::

## Configuration

```json
{
  "twitter": {
    "enabled": true,
    "token_env": "TWITTER_BEARER_TOKEN",
    "dm_token_env": "TWITTER_USER_TOKEN",
    "username": "your-handle",
    "user_id": "your-numeric-user-id",
    "watch_mentions": true,
    "watch_dms": false,
    "max_items": 100,
    "max_pages": 10
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `token_env` | `TWITTER_BEARER_TOKEN` | App bearer-token environment variable for mentions |
| `dm_token_env` | `TWITTER_USER_TOKEN` | User-context token environment variable for DMs |
| `username` | `""` | Handle used to resolve a user ID if `user_id` is empty |
| `user_id` | `""` | Numeric X user ID |
| `watch_mentions` | `true` | Poll the mentions timeline |
| `watch_dms` | `false` | Poll DM events; requires the user-context token |
| `max_items` | `100` | Endpoint page size, from 5 to 100 |
| `max_pages` | `10` | Mention page cap, from 1 to 10 |

Mentions paginate up to the configured cap. DMs intentionally fetch one page per poll and X exposes only its provider retention window (commonly up to 30 days for this endpoint), so prolonged downtime or a very large DM backlog can leave unrecoverable gaps.
