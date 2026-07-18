# Jira

Monitors Jira Cloud issues selected by a configured JQL expression and reports assignment, mention, and other tracked-state transitions.

## Pollen Types

| Type | Description |
|------|-------------|
| `jira_assigned` | The issue became assigned to `account_id` |
| `jira_mentioned` | Configured mention text newly appeared |
| `jira_updated` | Another tracked issue state changed |

## Setup

Create an Atlassian API token and export it. The scanner uses HTTP Basic auth with the configured account email and token.

```bash
export JIRA_TOKEN="your-api-token"
/hive hire jira
```

## Configuration

```json
{
  "jira": {
    "enabled": true,
    "token_env": "JIRA_TOKEN",
    "domain": "company.atlassian.net",
    "username": "you@example.com",
    "account_id": "your-atlassian-account-id",
    "jql": "assignee = currentUser() OR watcher = currentUser()",
    "mention_terms": ["@Your Name"],
    "jira_timezone": "UTC",
    "overlap_minutes": 10,
    "max_items": 100,
    "max_pages": 10
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `token_env` | `JIRA_TOKEN` | Token environment variable |
| `domain` | `""` | Jira Cloud hostname such as `company.atlassian.net` |
| `username` | `""` | Account email for Basic auth |
| `account_id` | `""` | Exact assignee account ID used to classify assignments |
| `jql` | built-in query | Base selection expression; HiveScanner adds its bounded update window |
| `mention_terms` | `[]` | Exact textual terms searched in issue description fields |
| `jira_timezone` | `UTC` | IANA timezone used when formatting the JQL update boundary |
| `overlap_minutes` | `10` | Search overlap, from 1 to 1,440 minutes |
| `max_items` | `100` | Page size, from 1 to 100 |
| `max_pages` | `10` | Page cap, from 1 to 10 |

See the [Jira Cloud REST API v3](https://developer.atlassian.com/cloud/jira/platform/rest/v3/). A full backlog that exceeds the cap is left uncommitted.
