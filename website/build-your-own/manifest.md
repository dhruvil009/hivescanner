# Manifest (teammate.json)

Every community scanner needs a `teammate.json` manifest alongside its adapter file.

## Example

```json
{
  "name": "rss",
  "display_name": "RSS Feeds",
  "version": "1.0.0",
  "description": "Monitors RSS/Atom feeds for new entries",
  "author": "hivescanner-community",
  "adapter_file": "adapter.py",
  "config_template": {
    "enabled": false,
    "feeds": [],
    "max_items_per_feed": 5
  },
  "requirements": {
    "cli_tools": []
  },
  "qpm_budget": 1
}
```

## Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique scanner identifier. Must match `^[a-zA-Z0-9_-]+$` |
| `display_name` | Yes | Human-readable name shown in the UI |
| `version` | Yes | Semver version string (e.g., `"1.0.0"`) |
| `description` | Yes | One-line description of what the scanner monitors |
| `author` | Yes | Your name or handle |
| `adapter_file` | Yes | Python file containing the Scanner class (usually `adapter.py`) |
| `config_template` | Yes | Default configuration merged into `config.json` on hire |
| `requirements.cli_tools` | Yes | List of CLI tools that must be installed (checked during hire) |
| `qpm_budget` | Yes | Queries-per-minute budget for rate limiting |

## Config Template

The `config_template` object defines the default configuration for your scanner. When a user runs `/hive hire your-scanner`, these defaults are merged into their `config.json`.

Always include `"enabled": false` so the scanner doesn't start polling until the user explicitly enables it.

For tokens, use env var references:

```json
{
  "enabled": false,
  "token_env": "YOUR_SERVICE_TOKEN",
  "max_items": 20
}
```

## QPM Budget

The `qpm_budget` field controls how many API requests your scanner is allowed per minute. Keep this low to be a good API citizen:

- `1` — for lightweight APIs (RSS, Hacker News)
- `2` — for most APIs (Linear, Jira, GitLab)
- `3` — for APIs that require multiple calls per poll (Slack with multiple channels)

## Directory Structure

Your scanner directory should look like:

```
community/your-scanner/
├── adapter.py       # Scanner implementation
└── teammate.json    # Manifest
```
