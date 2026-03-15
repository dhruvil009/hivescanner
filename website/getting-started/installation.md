# Installation

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and working
- Python 3.10 or later
- Git

## Install via Git Clone

```bash
git clone https://github.com/dhruvil009/hivescanner.git ~/.claude/plugins/hivescanner
```

## Install via Claude Code Plugin Manager

```bash
claude plugin add hivescanner
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
