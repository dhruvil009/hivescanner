# Linear

Monitors selected Linear issues and reports assignment or tracked-state transitions through the [Linear GraphQL API](https://linear.app/developers/graphql).

## Pollen Types

| Type | Description |
|------|-------------|
| `linear_issue_new` | A newly selected issue was created inside the overlap window |
| `issue_assigned` | The issue became assigned to `assignee_id` |
| `issue_updated` | Status, priority, or assignee changed |

## Setup

Create a personal API key in Linear's API settings and export it:

```bash
export LINEAR_API_KEY="lin_api_..."
/hive hire linear
```

## Configuration

```json
{
  "linear": {
    "enabled": true,
    "api_key_env": "LINEAR_API_KEY",
    "team_id": "",
    "assignee_id": "",
    "max_items": 50,
    "max_pages": 10,
    "overlap_minutes": 5
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `api_key_env` | `LINEAR_API_KEY` | API-key environment variable |
| `team_id` | `""` | Optional exact team ID filter |
| `assignee_id` | `""` | Optional exact assignee ID filter/classifier |
| `max_items` | `50` | Cursor page size, from 1 to 250 |
| `max_pages` | `10` | Cursor page cap, from 1 to 10 |
| `overlap_minutes` | `5` | Updated-time overlap, from 0 to 1,440 minutes |

The adapter sends filters as GraphQL variables, validates cursor progress, and preserves state when pagination cannot reach a safe end.
