# Discord

Polls the [Discord REST API v10](https://discord.com/developers/docs/reference) for DMs in explicit DM channels and mentions in explicit guild channels.

## Pollen Types

| Type | Description |
|------|-------------|
| `discord_dm` | A message in a configured DM channel |
| `discord_mention` | A configured guild-channel message mentions `user_id` |

## Setup

Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications), grant it View Channel and Read Message History access to each watched guild channel, and export its token:

```bash
export DISCORD_BOT_TOKEN="your-bot-token"
/hive hire discord
```

REST polling cannot discover a bot's DM channels, so copy every desired DM channel ID into `watch_dm_channels`. Bot access, guild permissions, and Discord's current intent/content rules determine which message fields are visible.

## Configuration

```json
{
  "discord": {
    "enabled": true,
    "token_env": "DISCORD_BOT_TOKEN",
    "watch_channels": ["guild-channel-id"],
    "watch_dm_channels": ["dm-channel-id"],
    "watch_dms": false,
    "user_id": "your-user-id",
    "bot_user_id": "the-bot-user-id",
    "max_messages": 100,
    "channels_per_poll": 10
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `token_env` | `DISCORD_BOT_TOKEN` | Bot-token environment variable |
| `watch_channels` | `[]` | Unique numeric guild channel IDs |
| `watch_dm_channels` | `[]` | Unique numeric DM channel IDs |
| `watch_dms` | `false` | Include the explicit DM list; with no DM IDs, no DMs can be scanned |
| `user_id` | `""` | Numeric account ID used for exact mention detection |
| `bot_user_id` | `""` | Optional bot ID whose own messages are ignored |
| `max_messages` | `100` | Per-request message cap, from 1 to 100 |
| `channels_per_poll` | `10` | Channel requests rotated through per poll, from 1 to 100 |

Large channel bursts are backfilled over later polls without advancing past an incomplete boundary.
