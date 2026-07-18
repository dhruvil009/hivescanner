# Google Chat Scanner

Monitors selected Google Chat spaces and direct-message spaces for DMs and mentions. Work is rotated across a bounded number of spaces per poll.

## Pollen Types

| Type | Description |
|------|-------------|
| `gchat_dm` | A new message in a direct-message space |
| `gchat_mention` | A new message mentions the configured user |

## Prerequisites

Install the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli), then authenticate:

```bash
gws auth setup
gws auth login
gws chat spaces list --format json
```

## Configuration

```json
{
  "gchat": {
    "enabled": false,
    "watch_spaces": [],
    "watch_dm_spaces": [],
    "watch_dms": true,
    "user_resource": "",
    "username": "",
    "max_messages": 20,
    "max_pages": 10,
    "spaces_per_poll": 5
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable this scanner |
| `watch_spaces` | `[]` | Unique Google Chat space names or IDs, such as `spaces/AAAA` |
| `watch_dm_spaces` | `[]` | Explicit direct-message space names or IDs |
| `watch_dms` | `true` | Discover accessible DM spaces and refresh that discovery daily |
| `user_resource` | `""` | Optional `users/...` resource used for exact mention matching |
| `username` | `""` | Optional username used for textual mention matching |
| `max_messages` | `20` | Page size, from 1 to 1,000 |
| `max_pages` | `10` | Message/discovery page cap, from 1 to 10 |
| `spaces_per_poll` | `5` | Spaces rotated through per poll, from 1 to 20 |

An empty explicit watch list does not mean “scan every room”; it only adds automatically discovered DMs when `watch_dms` is enabled.
