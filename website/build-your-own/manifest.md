# Manifest (`teammate.json`)

Every community scanner needs a manifest beside its adapter.

```json
{
  "name": "rss",
  "display_name": "RSS Feeds",
  "version": "2.0.0",
  "description": "Monitors RSS/Atom feeds for new entries",
  "author": "hivescanner-community",
  "adapter_file": "adapter.py",
  "config_template": {
    "enabled": false,
    "feeds": [],
    "max_items_per_feed": 20,
    "feeds_per_poll": 2
  },
  "requirements": {"cli_tools": []},
  "qpm_budget": 2
}
```

| Field | Requirement |
|---|---|
| `name` | Unique `A-Z`, `a-z`, digit, `_`, or `-` name, at most 64 characters; must match the directory and scanner class |
| `display_name` | Non-empty text, at most 100 characters |
| `version` | Exact `X.Y.Z` semantic version |
| `description` | Non-empty text, at most 500 characters |
| `author` | Non-empty text, at most 100 characters |
| `adapter_file` | A non-symlink Python filename inside the scanner directory |
| `config_template` | JSON object whose `enabled` default is `false`; credential fields ending in `_env` name valid environment variables |
| `requirements.cli_tools` | Must be `[]`; community subprocesses cannot require child CLI tools |
| `qpm_budget` | Integer from 1 to 60; advisory metadata, not parent-enforced throttling |
| `supports_check_acted` | Optional boolean; omit or use `false` unless the interface is implemented and reviewed |

The adapter itself must enforce its provider's request budget, pacing, pagination caps, `Retry-After` behavior, and total poll deadline. Hiring validates and copies the files but leaves the scanner disabled until the user explicitly configures and enables it.
