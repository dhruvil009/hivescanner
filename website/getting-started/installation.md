# Installation

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and working
- Python 3.10 or later
- Git
- No pip packages required — HiveScanner uses only the Python standard library

## Install (Clone + Run)

Clone the repo and open Claude Code from within it:

```bash
git clone https://github.com/dhruvil009/hivescanner.git ~/hivescanner
cd ~/hivescanner
claude
```

## Install as a Plugin (All Sessions)

To make HiveScanner available from any project directory, clone the repo and register it in your Claude Code settings (`~/.claude/settings.json`):

```bash
git clone https://github.com/dhruvil009/hivescanner.git ~/hivescanner
```

```json
{
  "plugins": ["~/hivescanner"]
}
```

This loads HiveScanner's `/hive` skill in every Claude Code session.

## Verify Installation

In a Claude Code session, run:

```
/hive
```

If the setup wizard launches, HiveScanner is installed correctly.

## Directory Structure

After installation, HiveScanner creates its state directory at `~/.hivescanner/`:

```
~/.hivescanner/
├── config.json      # Your scanner configuration
├── pollen.json      # All notifications with lifecycle state
├── watermarks.json  # Per-scanner high-water marks
├── audit.json       # Triage action audit log
├── .lock            # PID lockfile
├── scanners/        # Installed community scanner files
└── teammates/       # Community scanner manifests
```

This directory persists across sessions and is created automatically on first run.
