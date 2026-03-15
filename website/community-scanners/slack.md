# Slack

Monitors Slack channels and DMs for messages, mentions, and thread replies.

## Pollen Types

| Type | Description |
|------|-------------|
| `slack_dm` | A direct message was received |
| `slack_mention` | You were @mentioned in a channel |
| `slack_thread_reply` | A reply was posted in a thread you're in |

## Getting a Token {#getting-a-token}

You need a Slack Bot User OAuth Token:

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**
2. Choose **From scratch**, name it "HiveScanner", and select your workspace
3. Go to **OAuth & Permissions** in the sidebar
4. Under **Bot Token Scopes**, add:
   - `channels:history` — read channel messages
   - `channels:read` — list channels
   - `im:history` — read DMs
   - `im:read` — list DMs
   - `users:read` — resolve usernames
5. Click **Install to Workspace** and authorize
6. Copy the **Bot User OAuth Token** (starts with `xoxb-`)
7. Add to your shell profile:

```bash
export SLACK_TOKEN="xoxb-xxxxxxxxxxxx"
```

## Install

```
/hive hire slack
```

## Configuration

```json
{
  "slack": {
    "enabled": true,
    "token_env": "SLACK_TOKEN",
    "watch_channels": ["general", "engineering"],
    "watch_dms": true,
    "username": "your-slack-username",
    "max_messages": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `token_env` | `SLACK_TOKEN` | Environment variable containing your bot token |
| `watch_channels` | `[]` | Channel names to monitor |
| `watch_dms` | `true` | Monitor direct messages |
| `username` | `""` | Your Slack username (for filtering @mentions) |
| `max_messages` | `20` | Max messages fetched per poll cycle |

## API Details

Uses the [Slack Web API](https://api.slack.com/web) (`conversations.history`, `conversations.list`).
