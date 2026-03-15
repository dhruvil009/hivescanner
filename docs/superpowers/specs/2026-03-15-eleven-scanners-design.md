# Eleven New Scanners Design

**Goal:** Implement 11 new scanners — Email, Weather (built-in), PagerDuty, Sentry, Jira, GitLab, Notion, Telegram, Discord, HackerNews, Package Tracking (community).

**Architecture:** Same self-contained pattern as existing scanners. Built-ins use CLI/HTTP via subprocess/urllib. Community scanners use sandboxed JSON-over-stdio protocol.

## Built-in Scanners

### Email / Gmail (`workers/sources/email.py`)
- CLI: `gws gmail +triage --output json`
- Auth: `gws auth` (shared with Calendar/GChat)
- Pollen: `email_new`, `email_urgent` (VIP senders)
- Config: `enabled`, `vip_senders`, `max_emails`

### Weather (`workers/sources/weather.py`)
- API: `wttr.in/{location}?format=j1` via urllib.request
- Pollen: `weather_morning` (daily briefing), `weather_alert` (significant change)
- Config: `enabled`, `location`, `morning_hour`, `alert_temp_swing`

## Community Scanners

### PagerDuty (`community/pagerduty/`)
- API: REST v2, `PAGERDUTY_TOKEN`
- Pollen: `pagerduty_incident`, `pagerduty_triggered`

### Sentry (`community/sentry/`)
- API: REST, `SENTRY_TOKEN`
- Pollen: `sentry_issue`, `sentry_spike`

### Jira (`community/jira/`)
- API: REST v3, `JIRA_TOKEN` + `jira_domain`
- Pollen: `jira_assigned`, `jira_updated`, `jira_mentioned`

### GitLab (`community/gitlab/`)
- API: REST v4, `GITLAB_TOKEN`
- Pollen: `gitlab_mr_review`, `gitlab_ci_failure`, `gitlab_mention`

### Notion (`community/notion/`)
- API: REST, `NOTION_TOKEN`
- Pollen: `notion_page_updated`, `notion_comment`

### Telegram (`community/telegram/`)
- API: Bot API, `TELEGRAM_BOT_TOKEN`
- Pollen: `telegram_message`, `telegram_mention`

### Discord (`community/discord/`)
- API: Bot API, `DISCORD_BOT_TOKEN`
- Pollen: `discord_dm`, `discord_mention`

### HackerNews (`community/hackernews/`)
- API: Algolia (no key needed)
- Pollen: `hn_top_story`, `hn_mention`

### Package Tracking (`community/package-tracking/`)
- Gmail-based: parses shipping emails for tracking numbers, calls carrier API on demand
- Pollen: `package_shipped`, `package_out_for_delivery`, `package_delivered`
