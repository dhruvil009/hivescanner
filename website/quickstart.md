# Quick Start

Get HiveScanner running in under 5 minutes.

## 1. Install

Add the HiveScanner marketplace and install the plugin:

```bash
claude plugin marketplace add github:dhruvil009/hivescanner
claude plugin install hivescanner
```

Or clone the repo and load it directly for a single session:

```bash
git clone https://github.com/dhruvil009/hivescanner.git ~/hivescanner
claude --plugin-dir ~/hivescanner
```

See [Installation](/getting-started/installation) for more details.

## 2. Start the Setup Wizard

In your Claude Code session, run:

```
/hive
```

HiveScanner launches an interactive wizard that walks you through configuration.

## 3. Configure Your Sources

The wizard will ask you to set up:

- **GitHub** — which repos to watch, whether to track reviews, CI, and @mentions (requires `gh` CLI or `$GITHUB_TOKEN`)
- **Slack** — optional; which channels and DMs to monitor (requires `$SLACK_TOKEN`)
- **Calendar** — optional; Google Calendar integration for meeting reminders (requires `gws` CLI)
- **git_status** — enabled by default; watches local directories for uncommitted changes, stale branches, and merge conflicts

## 4. Set Your Poll Interval

Choose how frequently workers check for updates:

| Interval | Description |
|----------|-------------|
| Every 2 minutes | Aggressive — for oncall or high-urgency work |
| **Every 5 minutes** | **Recommended** — good balance of freshness and efficiency |
| Every 10 minutes | Relaxed — for low-traffic periods |

## 5. You're Done

HiveScanner starts running silently in the background. You'll see a status banner confirming your active scanners:

```
Initializing HiveScanner. Deploying workers to the field...
Scanners: github (3 repos, reviews on, CI on)
          git_status (2 dirs)
Poll interval: 5 min
Pending pollen: 0 from last session
```

Use `/hive status` to check health anytime. Use `/hive stop` to shut down.

## Next Steps

- [Install community scanners](/community-scanners/) like Linear, Jira, or PagerDuty
- [Configure built-in scanners](/built-in-scanners/) in detail
- [Build your own scanner](/build-your-own/scanner-interface)
