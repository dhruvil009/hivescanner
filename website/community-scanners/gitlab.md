# GitLab

Monitors GitLab merge request reviews, CI pipeline failures, and @mentions.

## Pollen Types

| Type | Description |
|------|-------------|
| `gitlab_mr_review` | A merge request needs your review |
| `gitlab_ci_failure` | A CI pipeline failed |
| `gitlab_mention` | You were @mentioned |

## Getting a Token

1. Go to [GitLab Personal Access Tokens](https://gitlab.com/-/user_settings/personal_access_tokens) (or your self-hosted GitLab instance)
2. Click **Add new token**
3. Give it a name (e.g., "HiveScanner")
4. Select scopes: `read_api`
5. Click **Create personal access token** and copy the token
6. Add to your shell profile:

```bash
export GITLAB_TOKEN="glpat-xxxxxxxxxxxx"
```

## Install

```
/hive hire gitlab
```

## Configuration

```json
{
  "gitlab": {
    "enabled": true,
    "token_env": "GITLAB_TOKEN",
    "gitlab_url": "https://gitlab.com",
    "username": "your-gitlab-username",
    "max_items": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `token_env` | `GITLAB_TOKEN` | Environment variable containing your token |
| `gitlab_url` | `https://gitlab.com` | GitLab instance URL (change for self-hosted) |
| `username` | `""` | Your GitLab username |
| `max_items` | `20` | Max items fetched per poll cycle |

## API Details

Uses the [GitLab REST API v4](https://docs.gitlab.com/ee/api/rest/). Supports both gitlab.com and self-hosted instances.
