# Sentry

Monitors Sentry for new issues and error spikes in your projects.

## Pollen Types

| Type | Description |
|------|-------------|
| `sentry_issue` | A new issue was detected |
| `sentry_spike` | An error spike was detected |

## Getting a Token

1. Go to [Sentry Auth Tokens](https://sentry.io/settings/auth-tokens/)
2. Click **Create New Token**
3. Select scopes: `project:read`, `event:read`
4. Copy the token and add to your shell profile:

```bash
export SENTRY_TOKEN="sntrys_xxxxxxxxxxxx"
```

## Install

```
/hive hire sentry
```

## Configuration

```json
{
  "sentry": {
    "enabled": true,
    "token_env": "SENTRY_TOKEN",
    "organization": "your-org-slug",
    "project": "your-project-slug",
    "max_items": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `token_env` | `SENTRY_TOKEN` | Environment variable containing your auth token |
| `organization` | `""` | Your Sentry organization slug (from the URL) |
| `project` | `""` | Your Sentry project slug (from the URL) |
| `max_items` | `20` | Max issues fetched per poll cycle |

## API Details

Uses the [Sentry REST API](https://docs.sentry.io/api/).
