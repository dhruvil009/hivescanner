# WhatsApp Scanner

Monitors WhatsApp for incoming messages from configured chats.

## Pollen Types

| Type | Description |
|------|-------------|
| `whatsapp_message` | An incoming WhatsApp message |

## Prerequisites

Requires [whatsapp-cli](https://github.com/nicehash/whatsapp-cli):

1. Install whatsapp-cli following its [setup instructions](https://github.com/nicehash/whatsapp-cli#installation)
2. Link your WhatsApp account by scanning the QR code
3. Verify it's working: `whatsapp-cli status`

## Configuration

Enable WhatsApp monitoring in your `~/.hivescanner/config.json`:

```json
{
  "whatsapp": {
    "enabled": true
  }
}
```

## Tips

- Configure specific chats to monitor to avoid being overwhelmed by group messages
- The scanner respects WhatsApp's rate limits automatically
