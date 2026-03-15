# Community Scanners

HiveScanner's community scanner ecosystem lets you extend monitoring to any service with an API. Community scanners run sandboxed in isolated subprocesses.

## Available Scanners

| Scanner | Description | Auth Required |
|---------|-------------|---------------|
| [Linear](/community-scanners/linear) | Issues and status changes | `LINEAR_API_KEY` |
| [Slack](/community-scanners/slack) | Channels, DMs, mentions, threads | `SLACK_TOKEN` |
| [Discord](/community-scanners/discord) | DMs and channel mentions | `DISCORD_BOT_TOKEN` |
| [Telegram](/community-scanners/telegram) | Messages and mentions via Bot API | `TELEGRAM_BOT_TOKEN` |
| [Jira](/community-scanners/jira) | Assigned, updated, and mentioned issues | `JIRA_TOKEN` |
| [GitLab](/community-scanners/gitlab) | MR reviews, CI failures, mentions | `GITLAB_TOKEN` |
| [PagerDuty](/community-scanners/pagerduty) | Incidents and triggered alerts | `PAGERDUTY_TOKEN` |
| [Sentry](/community-scanners/sentry) | Issues and error spikes | `SENTRY_TOKEN` |
| [Notion](/community-scanners/notion) | Page updates and comments | `NOTION_TOKEN` |
| [Twitter / X](/community-scanners/twitter) | Mentions and DMs | `TWITTER_BEARER_TOKEN` |
| [Facebook](/community-scanners/facebook) | Page notifications and Messenger | `FACEBOOK_TOKEN` |
| [RSS](/community-scanners/rss) | RSS/Atom feed monitoring | None |
| [Hacker News](/community-scanners/hackernews) | Top stories and username mentions | None |
| [Package Tracking](/community-scanners/package-tracking) | Shipping updates from Gmail | `GOOGLE_ACCESS_TOKEN` |

## Hire / Fire Lifecycle

### Install a scanner

```
/hive hire linear
```

This:
1. Copies `adapter.py` to `~/.hivescanner/scanners/`
2. Copies `teammate.json` to `~/.hivescanner/teammates/linear/`
3. Merges the scanner's `config_template` into your `config.json`

### Remove a scanner

```
/hive fire linear
```

Your configuration is backed up automatically. Re-hiring restores your previous settings.

### Re-hire a scanner

```
/hive hire linear
```

If you previously fired a scanner, re-hiring restores your saved configuration rather than starting fresh.

## Building Your Own

If a service has an API, you can build a scanner for it. See [Build Your Own Scanner](/build-your-own/scanner-interface).
