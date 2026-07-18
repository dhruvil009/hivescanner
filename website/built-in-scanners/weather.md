# Weather Scanner

Uses [wttr.in](https://wttr.in) for a daily local-time briefing and change alerts. No API key or extra executable is required.

## Pollen Types

| Type | Description |
|------|-------------|
| `weather_morning` | One briefing in the configured morning window |
| `weather_alert` | Temperature crossed the swing threshold or rain/snow began |

## Configuration

```json
{
  "weather": {
    "enabled": false,
    "location": "San Francisco, CA",
    "morning_hour": 8,
    "timezone": "America/Los_Angeles",
    "morning_window_hours": 4,
    "alert_temp_swing_c": 10
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable this scanner |
| `location` | `""` | Required wttr.in location query |
| `morning_hour` | `8` | Local start hour, from 0 to 23 |
| `timezone` | `""` | IANA timezone; empty uses the machine's local timezone |
| `morning_window_hours` | `4` | Briefing eligibility window, from 1 to 12 hours |
| `alert_temp_swing_c` | `10` | Absolute change threshold, from 0 to 100 °C; zero still requires an actual change |

Changing the location or timezone establishes a new quiet baseline. Provider failures, implausible values, redirects away from wttr.in, and malformed responses preserve the existing state.
