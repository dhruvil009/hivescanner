# Calendar Scanner

Monitors Google Calendar for upcoming meetings and event changes. Surfaces reminders at 30 minutes and 10 minutes before events.

## Pollen Types

| Type | Description |
|------|-------------|
| `meeting_reminder` | A meeting is starting in 30 or 10 minutes |
| `event_changed` | An event was added, modified, or cancelled |

## Prerequisites

Requires the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli):

1. Install `gws` following its [setup instructions](https://github.com/googleworkspace/cli#installation)
2. Authenticate with your Google account: `gws auth login`
3. Verify access: `gws calendar list`

## Configuration

```json
{
  "calendar": {
    "enabled": false,
    "provider": "google",
    "credentials_path": "",
    "prep_minutes_before": 30,
    "reminder_minutes_before": 10,
    "filter_declined": true,
    "noise_subjects": ["Focus Time", "Lunch", "OOO"]
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable/disable this scanner |
| `provider` | `google` | Calendar provider (currently only Google is supported) |
| `credentials_path` | — | Path to Google credentials file (if not using `gws` CLI) |
| `prep_minutes_before` | `30` | First reminder before event start |
| `reminder_minutes_before` | `10` | Second reminder before event start |
| `filter_declined` | `true` | Skip events you've declined |
| `noise_subjects` | `["Focus Time", "Lunch", "OOO"]` | Event subjects to ignore |

## Tips

- Add common filler events to `noise_subjects` to reduce notification noise
- The scanner only surfaces genuinely new or changed events — no duplicates between poll cycles
