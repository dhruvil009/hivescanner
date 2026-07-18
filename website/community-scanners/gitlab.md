# GitLab

Monitors review requests, failed pipelines, and GitLab todos through the [REST API v4](https://docs.gitlab.com/api/rest/). GitLab.com and HTTPS self-managed instances are supported.

## Pollen Types

| Type | Description |
|------|-------------|
| `gitlab_mr_review` | An open merge request requests your review |
| `gitlab_ci_failure` | A watched project's pipeline failed |
| `gitlab_mention` | A matching todo indicates an assignment or mention |

## Setup

Create a personal access token with sufficient read-API access and export it:

```bash
export GITLAB_TOKEN="glpat-..."
/hive hire gitlab
```

## Configuration

```json
{
  "gitlab": {
    "enabled": true,
    "token_env": "GITLAB_TOKEN",
    "gitlab_url": "https://gitlab.com",
    "username": "your-login",
    "watch_projects": ["group/project"],
    "watch_reviews": true,
    "watch_pipelines": true,
    "watch_todos": true,
    "max_items": 100,
    "max_pages": 3,
    "projects_per_poll": 3,
    "overlap_minutes": 10
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `token_env` | `GITLAB_TOKEN` | Token environment variable |
| `gitlab_url` | `https://gitlab.com` | HTTPS instance origin only; paths and query strings are rejected |
| `username` | `""` | GitLab username required for review filtering |
| `watch_projects` | `[]` | Unique `namespace/project` paths; needed for pipeline polling |
| `watch_reviews` / `watch_pipelines` / `watch_todos` | `true` | Enable each polling component |
| `max_items` | `100` | API page size, from 1 to 100 |
| `max_pages` | `3` | Page cap, from 1 to 5 |
| `projects_per_poll` | `3` | Projects rotated through for pipelines, from 1 to 10 |
| `overlap_minutes` | `10` | Pipeline time overlap, from 0 to 1,440 minutes |

Each component retains its own progress, so one failing endpoint does not make another endpoint's results appear fully committed.
