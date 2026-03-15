# Pollen Lifecycle

Every notification in HiveScanner is called a **pollen grain**. Pollen flows through a clear lifecycle from discovery to resolution.

## Lifecycle States

```
discovered → pending → acknowledged (dismissed)
                     → acted (you handled it externally)
```

| State | Description |
|-------|-------------|
| `pending` | New pollen waiting for your attention |
| `acknowledged` | You dismissed it ("got it", "dismiss") |
| `acted` | You handled it externally (e.g., merged the PR outside HiveScanner) |

## Key Behaviors

### Bootstrap Silence

On first run, HiveScanner snapshots the current state of all data sources **without surfacing anything**. Only genuinely new items discovered after the initial snapshot are shown. This prevents a flood of existing notifications on setup.

### Watermark-Based Incremental Polling

Each scanner tracks a **high-water mark** — typically an ISO timestamp of the most recent item seen. On each poll, only items newer than the watermark are fetched. This means:

- No duplicates between poll cycles
- No re-processing of old items
- Efficient API usage (fewer items fetched per call)

### Deduplication

Pollen is deduplicated by ID. If the same notification is fetched twice (e.g., a PR review request that persists), you'll only see it once.

### Smart Batch Grouping

When 5 or more pollen grains arrive from the same author in a single cycle, they're collapsed into a single summary instead of overwhelming your screen.

### Retention and Pruning

- **Pending pollen is never pruned** — it persists until you acknowledge or act on it
- **Acknowledged/acted pollen** is retained for 7 days (configurable via `queue_retention_days`), then automatically pruned
- Pruning runs at the start of each polling cycle

## Pollen Structure

Every pollen grain contains:

```json
{
  "id": "github-pr-review-12345",
  "source": "github",
  "type": "review_needed",
  "title": "Review requested: Add user auth",
  "preview": "alice requested your review on #42",
  "discovered_at": "2025-01-15T10:30:00Z",
  "author": "alice",
  "author_name": "Alice Smith",
  "group": "owner/repo",
  "url": "https://github.com/owner/repo/pull/42",
  "metadata": { ... }
}
```
