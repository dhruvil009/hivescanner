# Weather Scanner

Provides daily morning weather briefings and alerts for significant temperature swings.

## Pollen Types

| Type | Description |
|------|-------------|
| `weather_morning` | Daily morning weather briefing |
| `weather_alert` | Significant temperature swing alert |

## Prerequisites

None — this scanner uses [wttr.in](https://wttr.in), a free weather API that requires no API key or installation.

## Configuration

Enable weather monitoring in your `~/.hivescanner/config.json`:

```json
{
  "weather": {
    "enabled": true
  }
}
```

## Tips

- The morning briefing is surfaced once per day during your first session
- Temperature swing alerts trigger when conditions change significantly between poll cycles
