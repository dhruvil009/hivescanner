# Package Tracking

Parses shipping confirmation emails from Gmail to track package delivery status.

## Pollen Types

| Type | Description |
|------|-------------|
| `package_shipped` | A package has shipped |
| `package_out_for_delivery` | A package is out for delivery |
| `package_delivered` | A package has been delivered |

## Getting a Token

This scanner reads your Gmail to find shipping emails. It requires a Google access token:

1. Set up Google OAuth 2.0 credentials via the [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Enable the **Gmail API** for your project
3. Generate an access token with the `gmail.readonly` scope
4. Add to your shell profile:

```bash
export GOOGLE_ACCESS_TOKEN="your-google-access-token"
```

::: tip
If you already have the `gws` CLI set up for Calendar/Email scanners, you can reuse those credentials.
:::

## Install

```
/hive hire package-tracking
```

## Configuration

```json
{
  "package-tracking": {
    "enabled": true,
    "token_env": "GOOGLE_ACCESS_TOKEN",
    "max_items": 10,
    "search_query": "subject:(shipped OR tracking OR delivery OR out for delivery) newer_than:1d"
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `token_env` | `GOOGLE_ACCESS_TOKEN` | Environment variable for Gmail API access |
| `max_items` | `10` | Max emails to scan per poll cycle |
| `search_query` | *(see above)* | Gmail search query for finding shipping emails |

## API Details

Uses the [Gmail REST API v1](https://developers.google.com/gmail/api/reference/rest).
