# Discord

Monitors Discord DMs and channel mentions via the Bot API.

## Pollen Types

| Type | Description |
|------|-------------|
| `discord_dm` | A direct message was received |
| `discord_mention` | You were @mentioned in a channel |

## Getting a Token

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application**, name it "HiveScanner"
3. Go to **Bot** in the sidebar and click **Add Bot**
4. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent**
5. Click **Reset Token** and copy the token
6. Add to your shell profile:

```bash
export DISCORD_BOT_TOKEN="your-bot-token"
```

7. Invite the bot to your server using the OAuth2 URL Generator:
   - Go to **OAuth2 > URL Generator**
   - Select scopes: `bot`
   - Select permissions: `Read Messages/View Channels`, `Read Message History`
   - Visit the generated URL and add the bot to your server

## Install

```
/hive hire discord
```

## Configuration

```json
{
  "discord": {
    "enabled": true,
    "token_env": "DISCORD_BOT_TOKEN",
    "watch_channels": ["channel-id-1", "channel-id-2"],
    "watch_dms": true,
    "user_id": "your-discord-user-id",
    "max_messages": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `token_env` | `DISCORD_BOT_TOKEN` | Environment variable containing your bot token |
| `watch_channels` | `[]` | Channel IDs to monitor (right-click channel > Copy ID) |
| `watch_dms` | `true` | Monitor direct messages |
| `user_id` | `""` | Your Discord user ID (for filtering @mentions) |
| `max_messages` | `20` | Max messages fetched per poll cycle |

## API Details

Uses the [Discord REST API v10](https://discord.com/developers/docs/reference).
