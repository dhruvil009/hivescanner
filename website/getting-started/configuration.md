# Configuration

HiveScanner stores all configuration in `~/.hivescanner/config.json`. You can edit this file directly or use the `/hive` wizard.

## Config Structure

```json
{
  "version": 1,
  "poll_interval_seconds": 300,
  "queue_retention_days": 7,
  "user": {
    "username": "YOUR_USERNAME",
    "email": ""
  },
  "autonomy": {
    "enabled": false,
    "oncall_groups": []
  },
  "scanners": {
    "github": { ... },
    "slack": { ... },
    "calendar": { ... },
    "git_status": { ... }
  }
}
```

## Global Settings

| Field | Default | Description |
|-------|---------|-------------|
| `poll_interval_seconds` | `300` | How often workers poll for updates (in seconds) |
| `queue_retention_days` | `7` | How long acknowledged/acted pollen is retained before pruning |
| `user.username` | — | Your username (used for filtering @mentions) |
| `user.email` | — | Your email (used for calendar matching) |

## Autonomy Settings

Triage autonomy is off by default. See [Triage Autonomy](/concepts/triage-autonomy) for details.

| Field | Default | Description |
|-------|---------|-------------|
| `autonomy.enabled` | `false` | Global kill switch for auto-responses |
| `autonomy.oncall_groups` | `[]` | Groups/channels where auto-responses are allowed |

## Scanner Configuration

Each scanner has its own configuration block under `scanners`. See individual scanner pages for details:

- [GitHub](/built-in-scanners/github)
- [Git Status](/built-in-scanners/git-status)
- [Calendar](/built-in-scanners/calendar)
- [Google Chat](/built-in-scanners/gchat)
- [Email](/built-in-scanners/email)
- [WhatsApp](/built-in-scanners/whatsapp)
- [Weather](/built-in-scanners/weather)

## Environment Variables

HiveScanner uses environment variables for secrets. Tokens are never stored in config files — only the env var name is saved.

| Variable | Scanner | How to Get It |
|----------|---------|---------------|
| `GITHUB_TOKEN` | GitHub | `gh auth token` or [create a PAT](https://github.com/settings/tokens) |
| `SLACK_TOKEN` | Slack | [Create a Slack App](/community-scanners/slack#getting-a-token) |
| `LINEAR_API_KEY` | Linear | [Linear API Settings](https://linear.app/settings/api) |
| `JIRA_TOKEN` | Jira | [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `GITLAB_TOKEN` | GitLab | [GitLab Personal Access Tokens](https://gitlab.com/-/user_settings/personal_access_tokens) |
| `PAGERDUTY_TOKEN` | PagerDuty | [PagerDuty API Access Keys](https://support.pagerduty.com/main/docs/api-access-keys) |
| `SENTRY_TOKEN` | Sentry | [Sentry Auth Tokens](https://sentry.io/settings/auth-tokens/) |
| `NOTION_TOKEN` | Notion | [Notion Integrations](https://www.notion.so/my-integrations) |
| `TELEGRAM_BOT_TOKEN` | Telegram | [@BotFather on Telegram](https://t.me/BotFather) |
| `DISCORD_BOT_TOKEN` | Discord | [Discord Developer Portal](https://discord.com/developers/applications) |

Set them in your shell profile (e.g., `~/.zshrc`):

```bash
export GITHUB_TOKEN="ghp_xxxxxxxxxxxx"
export SLACK_TOKEN="xoxb-xxxxxxxxxxxx"
```
