# Jira

Monitors Jira for issues assigned to you, updates, and mentions.

## Pollen Types

| Type | Description |
|------|-------------|
| `jira_assigned` | An issue was assigned to you |
| `jira_mentioned` | You were mentioned in an issue |
| `jira_updated` | An assigned issue was updated |

## Getting a Token

1. Go to [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click **Create API token**
3. Give it a label (e.g., "HiveScanner")
4. Copy the token and add to your shell profile:

```bash
export JIRA_TOKEN="your-api-token"
```

## Install

```
/hive hire jira
```

## Configuration

```json
{
  "jira": {
    "enabled": true,
    "token_env": "JIRA_TOKEN",
    "domain": "your-company.atlassian.net",
    "username": "your-email@company.com",
    "max_items": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `token_env` | `JIRA_TOKEN` | Environment variable containing your API token |
| `domain` | `""` | Your Jira domain (e.g., `your-company.atlassian.net`) |
| `username` | `""` | Your Jira email address (used for Basic auth) |
| `max_items` | `20` | Max issues fetched per poll cycle |

## API Details

Uses the [Jira REST API v3](https://developer.atlassian.com/cloud/jira/platform/rest/v3/) with Basic authentication (email + API token).
