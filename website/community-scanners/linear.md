# Linear

Monitors Linear issues and status changes assigned to you.

## Pollen Types

| Type | Description |
|------|-------------|
| `issue_assigned` | An issue was assigned to you |
| `issue_updated` | An assigned issue's status changed |

## Getting a Token

1. Go to [Linear Settings > API](https://linear.app/settings/api)
2. Click **Create key** under "Personal API keys"
3. Give it a label (e.g., "HiveScanner")
4. Copy the key and add it to your shell profile:

```bash
export LINEAR_API_KEY="lin_api_xxxxxxxxxxxx"
```

## Install

```
/hive hire linear
```

## Configuration

```json
{
  "linear": {
    "enabled": true,
    "api_key_env": "LINEAR_API_KEY",
    "team_id": "your-team-id"
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `api_key_env` | `LINEAR_API_KEY` | Environment variable containing your API key |
| `team_id` | `""` | Your Linear team ID (find it in team settings URL) |

## API Details

Uses the [Linear GraphQL API](https://developers.linear.app/docs/graphql/working-with-the-graphql-api) with parameterized queries (no injection risk).
