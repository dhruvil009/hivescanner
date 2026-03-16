# Installation

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and working
- Python 3.10 or later
- Git

## Install via Plugin Marketplace (Recommended)

Add the HiveScanner repo as a marketplace source and install:

```bash
claude plugin marketplace add github:dhruvil009/hivescanner
claude plugin install hivescanner
```

This installs HiveScanner permanently and makes it available in all sessions.

## Install via --plugin-dir (Session Only)

Clone the repo anywhere and load it for a single session:

```bash
git clone https://github.com/dhruvil009/hivescanner.git ~/hivescanner
claude --plugin-dir ~/hivescanner
```

This is useful for trying HiveScanner before committing to a permanent install.

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
