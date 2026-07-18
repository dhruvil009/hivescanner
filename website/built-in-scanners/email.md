# Email Scanner

Monitors Gmail for messages matching a Gmail search query and marks exact configured sender addresses as urgent. The first successful poll establishes a quiet baseline.

## Pollen Types

| Type | Description |
|------|-------------|
| `email_new` | A newly observed matching message |
| `email_urgent` | A newly observed matching message from an exact VIP address |

## Prerequisites

Install the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli), then authenticate:

```bash
gws auth setup
gws auth login
gws gmail messages list --params '{"userId":"me","maxResults":1}' --format json
```

## Configuration

```json
{
  "email": {
    "enabled": false,
    "vip_senders": ["person@example.com"],
    "query": "in:inbox",
    "max_emails": 20,
    "max_pages": 5,
    "overlap_seconds": 300
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | Enable this scanner |
| `vip_senders` | `[]` | Unique email addresses matched exactly and case-insensitively |
| `query` | `in:inbox` | Gmail search expression combined with the scanner's time boundary |
| `max_emails` | `20` | Message details processed per poll, from 1 to 20 |
| `max_pages` | `5` | List-page cap, from 1 to 5 |
| `overlap_seconds` | `300` | Boundary overlap, from 60 to 3,600 seconds |

The list endpoint can cover at most five 500-message pages per poll. A larger unresolved backlog is detected and left uncommitted so it is not silently skipped; narrow `query` if that condition persists.
