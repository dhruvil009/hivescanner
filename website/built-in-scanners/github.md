# GitHub Scanner

Monitors your GitHub repositories for PR review requests, CI status changes, @mentions, and issue assignments.

## Pollen Types

| Type | Description |
|------|-------------|
| `review_needed` | A PR needs your review |
| `ci_failure` | CI failed on a branch you own |
| `ci_passed` | CI passed (after a previous failure) |
| `mention` | You were @mentioned in an issue or PR |
| `issue_assigned` | An issue was assigned to you |
| `notification` | General GitHub notification |

## Prerequisites

You need **one** of the following:

- **`gh` CLI** (recommended) — inherits your existing GitHub authentication
- **`GITHUB_TOKEN` environment variable** — a [Personal Access Token](https://github.com/settings/tokens)

### Using the `gh` CLI

1. Install: [cli.github.com](https://cli.github.com/)
2. Authenticate: `gh auth login`
3. Verify: `gh auth status`

HiveScanner will use `gh` automatically if it's installed and authenticated.

### Using a Personal Access Token

1. Go to [GitHub Settings > Developer Settings > Personal Access Tokens](https://github.com/settings/tokens)
2. Click **Generate new token (classic)**
3. Select scopes: `repo`, `notifications`
4. Copy the token and add to your shell profile:

```bash
export GITHUB_TOKEN="ghp_xxxxxxxxxxxx"
```

## Configuration

```json
{
  "github": {
    "enabled": true,
    "token_env": "GITHUB_TOKEN",
    "username": "your-github-username",
    "watch_repos": ["owner/repo1", "owner/repo2"],
    "watch_reviews": true,
    "watch_ci": true,
    "watch_mentions": true,
    "max_items_per_query": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `true` | Enable/disable this scanner |
| `token_env` | `GITHUB_TOKEN` | Environment variable containing your token |
| `username` | — | Your GitHub username (for filtering @mentions) |
| `watch_repos` | `[]` | List of `owner/repo` strings to monitor |
| `watch_reviews` | `true` | Surface PR review requests |
| `watch_ci` | `true` | Surface CI pass/fail notifications |
| `watch_mentions` | `true` | Surface @mention notifications |
| `max_items_per_query` | `20` | Max items fetched per poll cycle |
