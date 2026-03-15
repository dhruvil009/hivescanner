# Twitter / X

Monitors Twitter/X for mentions and direct messages.

## Pollen Types

| Type | Description |
|------|-------------|
| `twitter_mention` | You were mentioned in a tweet |
| `twitter_dm` | A direct message was received |

## Getting a Token

1. Go to the [X Developer Portal](https://developer.x.com/en/portal/dashboard)
2. Apply for a developer account if you don't have one
3. Create a new **Project** and **App**
4. Go to **Keys and tokens**
5. Generate a **Bearer Token**
6. Add to your shell profile:

```bash
export TWITTER_BEARER_TOKEN="your-bearer-token"
```

::: warning
The X API free tier has limited access. DM monitoring may require a paid tier.
:::

## Install

```
/hive hire twitter
```

## Configuration

```json
{
  "twitter": {
    "enabled": true,
    "token_env": "TWITTER_BEARER_TOKEN",
    "username": "your-twitter-handle",
    "user_id": "your-numeric-user-id",
    "watch_dms": true,
    "max_items": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `token_env` | `TWITTER_BEARER_TOKEN` | Environment variable containing your bearer token |
| `username` | `""` | Your Twitter/X handle (without @) |
| `user_id` | `""` | Your numeric user ID |
| `watch_dms` | `true` | Monitor direct messages |
| `max_items` | `20` | Max items fetched per poll cycle |

## API Details

Uses the [X API v2](https://developer.x.com/en/docs/x-api).
