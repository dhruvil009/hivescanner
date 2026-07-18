# Package Tracking

Searches Gmail for shipping messages, extracts common carrier/tracking patterns, and classifies delivery language. It reads email; it does not query a carrier for authoritative package status.

## Pollen Types

| Type | Description |
|------|-------------|
| `package_shipped` | Message text indicates shipment |
| `package_out_for_delivery` | Message text indicates out for delivery |
| `package_delivered` | Message text indicates delivery |
| `package_update` | Other matching shipping update |

## Setup

Obtain a Google OAuth access token with Gmail read access and export the raw token expected by this adapter:

```bash
export GOOGLE_ACCESS_TOKEN="your-current-access-token"
/hive hire package-tracking
```

An authenticated `gws` credential store is not read by this community scanner. Ordinary access tokens expire, so a production setup needs an external refresh mechanism that updates the environment presented to HiveScanner.

## Configuration

```json
{
  "package-tracking": {
    "enabled": true,
    "token_env": "GOOGLE_ACCESS_TOKEN",
    "max_items": 20,
    "max_pages": 5,
    "overlap_seconds": 300,
    "search_query": "subject:(shipped OR tracking OR delivery OR out for delivery)"
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `token_env` | `GOOGLE_ACCESS_TOKEN` | Raw OAuth bearer-token environment variable |
| `max_items` | `20` | Message details processed per poll, from 1 to 100 |
| `max_pages` | `5` | Gmail list-page cap, from 1 to 10 |
| `overlap_seconds` | `300` | Search overlap, from 60 to 3,600 seconds |
| `search_query` | built-in shipping query | Gmail search expression combined with the scanner boundary |

See the [Gmail API](https://developers.google.com/gmail/api/reference/rest). A backlog beyond the page cap remains uncommitted instead of being silently skipped.
