# PagerDuty

Monitors active PagerDuty incidents and tracked incidents' terminal/filter transitions through the [REST API v2](https://developer.pagerduty.com/api-reference/).

## Pollen Types

| Type | Description |
|------|-------------|
| `pagerduty_triggered` | A matching incident is in triggered state |
| `pagerduty_incident` | Another active matching incident transition |
| `pagerduty_resolved` | A tracked incident resolved |
| `pagerduty_unassigned` | It no longer includes the configured user |
| `pagerduty_no_longer_matching` | It was deleted or left configured team/service filters |

## Setup

Create a PagerDuty API user token with incident read access and export it:

```bash
export PAGERDUTY_TOKEN="your-token"
/hive hire pagerduty
```

## Configuration

```json
{
  "pagerduty": {
    "enabled": true,
    "token_env": "PAGERDUTY_TOKEN",
    "user_id": "",
    "team_ids": [],
    "service_ids": [],
    "max_items": 100,
    "max_pages": 10
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `token_env` | `PAGERDUTY_TOKEN` | API-token environment variable |
| `user_id` | `""` | Optional PagerDuty user/assignee filter |
| `team_ids` | `[]` | Optional unique team ID filter |
| `service_ids` | `[]` | Optional unique service ID filter |
| `max_items` | `100` | Incident page size, from 1 to 100 |
| `max_pages` | `10` | Page cap, from 1 to 10 |

The scanner retains at most 100 active incidents and checks at most ten disappeared incidents in detail per poll. Use PagerDuty webhooks for larger or latency-sensitive installations.
