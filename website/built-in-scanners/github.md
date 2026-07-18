# GitHub Scanner

Monitors GitHub notifications and CI status on your open pull requests. The first successful poll establishes a baseline without replaying old activity.

## Pollen Types

| Type | Description |
|------|-------------|
| `review_needed` | A notification requests your review |
| `ci_failure` | CI on one of your open pull requests changed to failing |
| `ci_passed` | CI recovered after a previously observed failure |
| `mention` | A notification mentions you or your team |
| `issue_assigned` | A notification assigns an issue or pull request to you |
| `notification` | Other watched GitHub activity |

## Prerequisites

The scanner always invokes the [GitHub CLI (`gh`)](https://cli.github.com/). Install it, then either authenticate with `gh auth login` or provide a token through the configured `token_env`. A token does not replace the `gh` executable.

```bash
gh auth login
gh auth status
```

If `GITHUB_TOKEN` is present, HiveScanner passes it to `gh` as `GH_TOKEN` for that invocation and otherwise uses the CLI's existing credential store.

## Configuration

```json
{
  "github": {
    "enabled": true,
    "token_env": "GITHUB_TOKEN",
    "username": "your-github-login",
    "watch_repos": ["owner/repository"],
    "watch_reviews": true,
    "watch_ci": true,
    "watch_mentions": true,
    "watch_assignments": true,
    "watch_activity": false,
    "max_items_per_query": 20,
    "max_notification_pages": 10,
    "max_pr_pages": 10
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `true` | Enable this scanner |
| `token_env` | `GITHUB_TOKEN` | Optional environment variable used by `gh` |
| `username` | `""` | GitHub login used to identify your pull requests; the global HiveScanner username is the fallback |
| `watch_repos` | `[]` | Optional unique `owner/repository` allowlist; empty means all accessible repositories |
| `watch_reviews` | `true` | Surface review-request notifications |
| `watch_ci` | `true` | Track CI transitions on your open pull requests |
| `watch_mentions` | `true` | Surface user and team mentions |
| `watch_assignments` | `true` | Surface assignments |
| `watch_activity` | `false` | Surface other notification reasons |
| `max_items_per_query` | `20` | Page size, from 1 to 100 |
| `max_notification_pages` | `10` | Notification page cap, from 1 to 10 |
| `max_pr_pages` | `10` | Open-pull-request page cap, from 1 to 10 |

When a result cannot be paged to a safe boundary, the affected component preserves its prior cursor and tries again later instead of silently skipping records.
