# Sentry

Monitors a Sentry organization/project issue query for newly tracked issues, count spikes, and terminal transitions.

## Pollen Types

| Type | Description |
|------|-------------|
| `sentry_issue` | A newly tracked issue |
| `sentry_spike` | Event count crossed the configured delta and ratio |
| `sentry_resolved` | A tracked issue resolved |
| `sentry_ignored` | A tracked issue became ignored |
| `sentry_no_longer_matching` | A tracked issue left the configured query/project |

## Setup

Create a Sentry auth token with appropriate organization/project and issue/event read scopes, then export it:

```bash
export SENTRY_TOKEN="sntrys_..."
/hive hire sentry
```

## Configuration

```json
{
  "sentry": {
    "enabled": true,
    "token_env": "SENTRY_TOKEN",
    "organization": "org-slug",
    "project": "project-slug",
    "query": "is:unresolved",
    "min_event_delta": 10,
    "spike_ratio": 2.0,
    "max_items": 100,
    "max_pages": 10
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `token_env` | `SENTRY_TOKEN` | Auth-token environment variable |
| `organization` | `""` | Required organization slug |
| `project` | `""` | Optional project slug |
| `query` | `is:unresolved` | Sentry issue-search expression |
| `min_event_delta` | `10` | Minimum count increase required for a spike |
| `spike_ratio` | `2.0` | Minimum new/old count ratio required for a spike |
| `max_items` | `100` | Issue page size, from 1 to 100 |
| `max_pages` | `10` | Page cap, from 1 to 10 |

The scanner tracks at most 500 issues and checks at most ten disappeared issues in detail per poll. See the [Sentry API](https://docs.sentry.io/api/); use webhooks beyond these polling bounds.
