# Calendar Scanner

Monitors Google Calendar for changed events and configurable pre-meeting reminders. The first successful poll establishes a quiet baseline.

## Pollen Types

| Type | Description |
|------|-------------|
| `meeting_reminder` | An event entered a configured reminder threshold |
| `event_changed` | An event was added, changed, or cancelled after the baseline |

## Prerequisites

Install the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli), then follow its current authentication flow:

```bash
gws auth setup
gws auth login
gws calendar calendarList list --format json
```

The CLI is under active development, so re-check its upstream setup instructions after upgrading.

## Configuration

```json
{
  "calendar": {
    "enabled": false,
    "reminder_minutes": [30, 10],
    "max_events": 1000,
    "max_pages": 10,
    "lookahead_days": 30,
    "calendars": [],
    "timezone": "",
    "filter_declined": true,
    "noise_subjects": ["Focus Time", "Lunch", "OOO"]
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable this scanner |
| `reminder_minutes` | `[30, 10]` | Unique positive reminder thresholds in minutes |
| `max_events` | `1000` | Total event safety cap, from 1 to 5,000 |
| `max_pages` | `10` | Per-calendar and calendar-list page cap, from 1 to 100 |
| `lookahead_days` | `30` | Future window, from 1 to 90 days |
| `calendars` | `[]` | Calendar IDs to scan; empty discovers up to 25 accessible calendars |
| `timezone` | `""` | Optional IANA timezone passed to Calendar; empty uses API defaults |
| `filter_declined` | `true` | Ignore events you declined |
| `noise_subjects` | built-in list | Exact event summaries to ignore |

If the configured page or event cap is exhausted, the scanner preserves its state rather than claiming the truncated response is complete.
