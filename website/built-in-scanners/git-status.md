# Git Status Scanner

Monitors local Git repositories for old uncommitted work, remote-behind branches, stashes, and merge conflicts. Repositories are rotated across polls to keep work bounded.

## Pollen Types

| Type | Description |
|------|-------------|
| `uncommitted_warning` | Tracked or untracked changes remained dirty past the configured age |
| `branch_behind` | The current branch is behind its configured upstream |
| `stash_reminder` | The repository has a stash |
| `merge_conflict` | Git reports unmerged paths |

## Prerequisites

The `git` executable must be available. No network request is made by the scanner: behind status uses existing local remote-tracking refs, so run `git fetch` separately when freshness matters.

## Configuration

```json
{
  "git_status": {
    "enabled": true,
    "watch_dirs": ["."],
    "warn_uncommitted_after_minutes": 60,
    "warn_branch_behind": true,
    "repos_per_poll": 5
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `true` | Enable this scanner |
| `watch_dirs` | `["."]` | Up to 100 unique relative, absolute, or `~`-prefixed directories |
| `warn_uncommitted_after_minutes` | `60` | Dirty-age threshold; zero reports on the first post-baseline dirty observation |
| `warn_branch_behind` | `true` | Compare HEAD with its local upstream ref |
| `repos_per_poll` | `5` | Repositories checked per poll, from 1 to 20 |

Temporarily unavailable configured paths retain their dirty-age state, avoiding a false reset when a mount disappears.
