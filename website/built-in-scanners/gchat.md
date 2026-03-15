# Google Chat Scanner

Monitors Google Chat for direct messages and @mentions in configured spaces.

## Pollen Types

| Type | Description |
|------|-------------|
| `gchat_dm` | A direct message was received |
| `gchat_mention` | You were @mentioned in a space |

## Prerequisites

Requires the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli):

1. Install `gws` following its [setup instructions](https://github.com/googleworkspace/cli#installation)
2. Authenticate with your Google account: `gws auth login`
3. Verify access: `gws chat spaces list`

## Configuration

Enable Google Chat monitoring in your `~/.hivescanner/config.json`:

```json
{
  "gchat": {
    "enabled": true
  }
}
```

The Google Chat scanner uses the `gws` CLI for authentication and API access. No separate token is needed.
