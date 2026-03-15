# Git Status Scanner

Monitors your local Git repositories for uncommitted changes, branches that are behind remote, forgotten stashes, and merge conflicts.

## Pollen Types

| Type | Description |
|------|-------------|
| `uncommitted_warning` | You have uncommitted changes older than the configured threshold |
| `branch_behind` | Your branch is behind the remote |
| `stash_reminder` | You have stashed changes that may have been forgotten |
| `merge_conflict` | A merge conflict was detected |

## Prerequisites

None — this scanner works entirely locally using Git commands.

## Configuration

```json
{
  "git_status": {
    "enabled": true,
    "watch_dirs": ["."],
    "warn_uncommitted_after_minutes": 60,
    "warn_branch_behind": true
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `true` | Enable/disable this scanner |
| `watch_dirs` | `["."]` | List of directories to monitor (relative or absolute paths) |
| `warn_uncommitted_after_minutes` | `60` | Minutes before warning about uncommitted changes |
| `warn_branch_behind` | `true` | Warn when your branch is behind the remote |

## Tips

- Add multiple directories to `watch_dirs` to monitor several repos at once
- Set `warn_uncommitted_after_minutes` higher if you frequently work on long-running changes
- This scanner is enabled by default — it requires no tokens or external tools
