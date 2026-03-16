# Installation

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) v1.0.33+ installed and working
- Python 3.10 or later
- No pip packages required — HiveScanner uses only the Python standard library

## Install via Colony Marketplace (Recommended)

Add [Colony](https://github.com/dhruvil009/Colony), the plugin marketplace, and install HiveScanner from it. This gives you access to all Colony plugins and automatic updates:

```
/plugin marketplace add dhruvil009/Colony
/plugin install hivescanner@dhruvil009-Colony
```

Browse all available plugins with `/plugin` under the **Discover** tab, or visit [Colony on GitHub](https://github.com/dhruvil009/Colony).

## Install Standalone

If you only want HiveScanner without the full marketplace:

```
/plugin marketplace add dhruvil009/hivescanner
/plugin install hivescanner@dhruvil009-hivescanner
```

This loads HiveScanner's `/hive` skill in every Claude Code session.

## Manage Your Installation

```
/plugin                              # Browse and manage plugins (interactive UI)
/plugin disable hivescanner@...      # Temporarily disable
/plugin enable hivescanner@...       # Re-enable
/plugin uninstall hivescanner@...    # Remove completely
```

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
