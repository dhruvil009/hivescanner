# PagerDuty

Monitors PagerDuty for triggered incidents and alerts.

## Pollen Types

| Type | Description |
|------|-------------|
| `pagerduty_triggered` | A new incident was triggered |
| `pagerduty_incident` | An incident update or status change |

## Getting a Token

1. Log in to PagerDuty
2. Go to **My Profile > User Settings**
3. Under **API Access**, click **Create API User Token**
4. Copy the token and add to your shell profile:

```bash
export PAGERDUTY_TOKEN="your-pagerduty-token"
```

5. Find your User ID: Go to **My Profile** — the user ID is in the URL (e.g., `PXXXXXX`)

## Install

```
/hive hire pagerduty
```

## Configuration

```json
{
  "pagerduty": {
    "enabled": true,
    "token_env": "PAGERDUTY_TOKEN",
    "user_id": "PXXXXXX",
    "max_items": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `token_env` | `PAGERDUTY_TOKEN` | Environment variable containing your API token |
| `user_id` | `""` | Your PagerDuty user ID |
| `max_items` | `20` | Max incidents fetched per poll cycle |

## API Details

Uses the [PagerDuty REST API v2](https://developer.pagerduty.com/docs/rest-api-v2/rest-api/).
