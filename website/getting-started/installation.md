# Installation

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and working
- Python 3.10 or later
- Git
- No pip packages required — HiveScanner uses only the Python standard library

## Install via Colony Marketplace (Recommended)

Install through [Colony](https://github.com/dhruvil009/Colony), the plugin marketplace. This gives you HiveScanner plus access to all other Colony plugins and future updates:

```bash
git clone https://github.com/dhruvil009/Colony.git ~/Colony
```

Then register the plugin in your Claude Code settings (`~/.claude/settings.json`):

```json
{
  "plugins": ["~/Colony/plugins/hivescanner"]
}
```

Browse all available plugins at [Colony](https://github.com/dhruvil009/Colony).

## Install Standalone

If you only want HiveScanner, clone the repo directly and register it:

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
