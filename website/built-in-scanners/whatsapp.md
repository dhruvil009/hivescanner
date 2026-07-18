# WhatsApp Scanner

Monitors incoming messages stored locally by [whatsapp-cli](https://github.com/vicentereig/whatsapp-cli). The scanner queries the CLI's SQLite store; a separate long-running sync process is responsible for receiving messages.

## Pollen Types

| Type | Description |
|------|-------------|
| `whatsapp_message` | A newly observed incoming WhatsApp message |

## Prerequisites

Follow the project's current installation instructions, authenticate by QR code, then keep synchronization running:

```bash
whatsapp-cli auth
whatsapp-cli sync
```

If you select a custom database, pass the same `--store` path to authentication, sync, and HiveScanner's `store_path` configuration. Protect that file: it contains sensitive local message/account data.

::: warning
`whatsapp-cli` is an unofficial client, not a Meta/WhatsApp API. Its compatibility, account-policy implications, and ban risk are outside HiveScanner's control. Review both projects and WhatsApp's current terms before linking an important account.
:::

## Configuration

```json
{
  "whatsapp": {
    "enabled": false,
    "watch_chats": [],
    "max_messages": 20,
    "max_pages_per_poll": 100,
    "store_path": ""
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable this scanner |
| `watch_chats` | `[]` | Unique chat JIDs to allow; empty accepts all chats |
| `max_messages` | `20` | Legacy page-size component, from 1 to 500 |
| `max_pages_per_poll` | `100` | Legacy page-count component, from 1 to 100 |
| `store_path` | `""` | Optional path passed as `whatsapp-cli --store`; empty uses its default |

The scanner performs one stable newest-first query capped at `min(max_messages * max_pages_per_poll, 5000)` records. If an extreme backlog fills that budget before the prior boundary is reached, state is preserved and the poll is retried rather than skipping records.
