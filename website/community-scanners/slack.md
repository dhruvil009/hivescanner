# Slack

Polls explicit Slack channels and discovered DMs for messages, exact user-ID mentions, and thread replies visible in channel history.

## Pollen Types

| Type | Description |
|------|-------------|
| `slack_dm` | A new message in a discovered DM |
| `slack_mention` | A configured channel message contains `<@user_id>` |
| `slack_thread_reply` | A thread reply is visible in channel history |

## Setup

Create a Slack app and bot token with the history/read scopes needed for the channel types you monitor (commonly `channels:history`, `im:history`, and `im:read`), invite it to watched channels, and export the token:

```bash
export SLACK_TOKEN="xoxb-..."
/hive hire slack
```

## Configuration

```json
{
  "slack": {
    "enabled": true,
    "token_env": "SLACK_TOKEN",
    "watch_channels": ["C0123456789"],
    "watch_dms": true,
    "user_id": "U0123456789",
    "max_messages": 15,
    "history_requests_per_poll": 1,
    "allow_high_tier_rate_limits": false,
    "dm_discovery_max_pages": 10
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `token_env` | `SLACK_TOKEN` | Bot-token environment variable |
| `watch_channels` | `[]` | Up to 200 unique channel IDs, not names |
| `watch_dms` | `true` | Discover and include DM conversations |
| `user_id` | `""` | Exact Slack member ID used for mentions and self-message filtering |
| `max_messages` | `15` | History page size, from 1 to 15 |
| `history_requests_per_poll` | `1` | Channel-history requests per poll, from 1 to 50 |
| `allow_high_tier_rate_limits` | `false` | Permit more than one history request per poll only when your app tier supports it |
| `dm_discovery_max_pages` | `10` | DM discovery page cap, from 1 to 100 |

[Slack currently applies a 1 request/minute, 15-object limit](https://docs.slack.dev/reference/methods/conversations.history/) to some commercially distributed non-Marketplace apps. The conservative defaults match that tier; only opt into higher throughput when your app's actual tier allows it.
