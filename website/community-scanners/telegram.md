# Telegram

Monitors Telegram messages and mentions via the Bot API.

## Pollen Types

| Type | Description |
|------|-------------|
| `telegram_mention` | You were mentioned in a group chat |
| `telegram_message` | A message was received in a watched chat |

## Getting a Token

1. Open Telegram and search for [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts to create a bot
3. BotFather will give you a token like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`
4. Add to your shell profile:

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
```

5. Add the bot to any group chats you want to monitor

## Install

```
/hive hire telegram
```

## Configuration

```json
{
  "telegram": {
    "enabled": true,
    "token_env": "TELEGRAM_BOT_TOKEN",
    "watch_chats": [],
    "max_messages": 20
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `token_env` | `TELEGRAM_BOT_TOKEN` | Environment variable containing your bot token |
| `watch_chats` | `[]` | Chat IDs to monitor (leave empty for all) |
| `max_messages` | `20` | Max messages fetched per poll cycle |

## API Details

Uses the [Telegram Bot API](https://core.telegram.org/bots/api) (`getUpdates` endpoint).
