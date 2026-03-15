# Built-in Scanners

HiveScanner ships with 7 built-in scanners that cover the most common developer notification sources.

| Scanner | What It Watches | Auth Required |
|---------|----------------|---------------|
| [GitHub](/built-in-scanners/github) | PR reviews, CI status, @mentions, issue assignments | `gh` CLI or `GITHUB_TOKEN` |
| [Git Status](/built-in-scanners/git-status) | Uncommitted changes, stale branches, merge conflicts, stashes | None (local) |
| [Calendar](/built-in-scanners/calendar) | Meeting reminders, new/changed events | `gws` CLI |
| [Google Chat](/built-in-scanners/gchat) | DMs and @mentions in configured spaces | `gws` CLI |
| [Email](/built-in-scanners/email) | New emails, VIP sender urgent alerts via Gmail | `gws` CLI |
| [WhatsApp](/built-in-scanners/whatsapp) | Incoming messages from configured chats | `whatsapp-cli` |
| [Weather](/built-in-scanners/weather) | Daily morning briefing, temperature swing alerts | None (wttr.in) |

## Enabling a Scanner

Scanners are configured via the `/hive` wizard or by editing `~/.hivescanner/config.json` directly. Each scanner has an `enabled` field that controls whether it runs during polling cycles.

## External Dependencies

Some scanners require external CLI tools:

| Tool | Required By | Install |
|------|------------|---------|
| `gh` | GitHub | [cli.github.com](https://cli.github.com/) |
| `gws` | Calendar, Google Chat, Email | [Google Workspace CLI](https://github.com/googleworkspace/cli) |
| `whatsapp-cli` | WhatsApp | [whatsapp-cli](https://github.com/nicehash/whatsapp-cli) |
