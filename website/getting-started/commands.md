# Commands

All HiveScanner commands are accessed through the `/hive` slash command in Claude Code.

## Core Commands

| Command | Description |
|---------|-------------|
| `/hive` | Launch the setup wizard (first run) or show status |
| `/hive status` | Show scanner health, pending pollen count, and poll interval |
| `/hive stop` | Stop all background scanners |

## Pollen Management

| Command | Description |
|---------|-------------|
| `/hive dismiss` | Dismiss current pending pollen |
| `/hive dismiss all` | Dismiss all pending pollen |

You can also use natural language to interact with pollen — "got it", "dismiss all", "what did Jane post about?" — and the Queen will handle it.

## Community Scanner Management

| Command | Description |
|---------|-------------|
| `/hive hire <name>` | Install and activate a community scanner |
| `/hive fire <name>` | Remove a community scanner (config is backed up) |

Re-hiring a previously fired scanner restores your previous configuration.

## Autonomy Controls

| Command | Description |
|---------|-------------|
| `/hive autonomy on` | Enable triage auto-responses (requires additional safety gates) |
| `/hive autonomy off` | Immediately disable all auto-responses |

See [Triage Autonomy](/concepts/triage-autonomy) for details on the 6-gate safety system.
