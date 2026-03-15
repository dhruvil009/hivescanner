# Facebook

Monitors Facebook page notifications and Messenger messages.

## Pollen Types

| Type | Description |
|------|-------------|
| `facebook_notification` | A notification on a watched page |
| `facebook_message` | A Messenger message was received |

## Getting a Token

1. Go to [Meta for Developers](https://developers.facebook.com/)
2. Create a new app (type: Business)
3. Add the **Pages** and **Messenger** products
4. Go to **Tools > Graph API Explorer**
5. Select your app and generate a **Page Access Token** with permissions:
   - `pages_read_engagement`
   - `pages_messaging`
6. Copy the token and add to your shell profile:

```bash
export FACEBOOK_TOKEN="your-page-access-token"
```

::: tip
Page access tokens expire. For a long-lived token, exchange it via the [token debugger](https://developers.facebook.com/tools/debug/accesstoken/).
:::

## Install

```
/hive hire facebook
```

## Configuration

```json
{
  "facebook": {
    "enabled": true,
    "token_env": "FACEBOOK_TOKEN",
    "watch_pages": ["page-id-1"],
    "max_items": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `token_env` | `FACEBOOK_TOKEN` | Environment variable containing your page access token |
| `watch_pages` | `[]` | Facebook page IDs to monitor |
| `max_items` | `20` | Max items fetched per poll cycle |

## API Details

Uses the [Facebook Graph API v19.0](https://developers.facebook.com/docs/graph-api/).
