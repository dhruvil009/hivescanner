# Telegram

Polls the [Telegram Bot API](https://core.telegram.org/bots/api) `getUpdates` method for bot-visible messages and mentions.

## Pollen Types

| Type | Description |
|------|-------------|
| `telegram_mention` | Text mentions/replies to the configured bot identity |
| `telegram_message` | Another message in an allowed chat |

## Setup

Create a bot with [@BotFather](https://t.me/BotFather), configure its privacy settings for the visibility you require, add it to the desired chats, and export the token:

```bash
export TELEGRAM_BOT_TOKEN="123456:..."
/hive hire telegram
```

## Configuration

```json
{
  "telegram": {
    "enabled": true,
    "token_env": "TELEGRAM_BOT_TOKEN",
    "watch_chats": [],
    "max_messages": 20,
    "bot_username": "my_bot",
    "bot_user_id": "123456"
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `token_env` | `TELEGRAM_BOT_TOKEN` | Bot-token environment variable |
| `watch_chats` | `[]` | Unique numeric chat IDs; empty accepts all bot-visible chats |
| `max_messages` | `20` | `getUpdates` limit, from 1 to 100 |
| `bot_username` | `""` | Bot username without `@`, used for mention matching |
| `bot_user_id` | `""` | Numeric bot user ID, used for entities/reply matching and self filtering |

Telegram retains unconfirmed updates for no longer than 24 hours. Downtime beyond that provider window cannot be recovered by a polling adapter. Telegram also randomizes the next update ID after a quiet week; the scanner performs a no-offset queue probe before that boundary so a lower random ID is not accidentally acknowledged unseen. Ensure no webhook is configured for the same bot, because `getUpdates` and webhooks are mutually exclusive.
