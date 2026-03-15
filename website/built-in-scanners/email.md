# Email Scanner

Monitors Gmail for new emails and urgent messages from VIP senders.

## Pollen Types

| Type | Description |
|------|-------------|
| `email_new` | A new email arrived |
| `email_urgent` | An urgent email from a VIP sender |

## Prerequisites

Requires the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli):

1. Install `gws` following its [setup instructions](https://github.com/googleworkspace/cli#installation)
2. Authenticate with your Google account: `gws auth login`
3. Verify access: `gws gmail messages list`

## Configuration

Enable email monitoring in your `~/.hivescanner/config.json`:

```json
{
  "email": {
    "enabled": true
  }
}
```

The Email scanner uses the `gws` CLI for authentication and Gmail API access. No separate token is needed.

## Tips

- Configure VIP senders to get urgent notifications for critical contacts
- The scanner fetches only new emails since the last watermark — no duplicates
