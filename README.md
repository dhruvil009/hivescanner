# HiveScanner

**Your IDE is your command center.**

<!-- Badges -->
![Version](https://img.shields.io/badge/version-1.0.0-blue)
![License](https://img.shields.io/badge/license-Apache%202.0%20%2B%20Commons%20Clause-green)
![Platform](https://img.shields.io/badge/platform-Claude%20Code-purple)
![Python](https://img.shields.io/badge/python-3.10%2B-yellow)

Not another dashboard. Not another Slack bot. Not another standalone agent you run in a separate terminal. HiveScanner is a Claude Code plugin that turns your coding environment into a monitoring command center -- silently polling GitHub, Slack, Calendar, and your local repos in the background while you code. When something needs your attention, it surfaces right where you already are. When nothing's happening, it burns zero LLM tokens. Your IDE becomes the single pane of glass for everything that matters.

---

## What is HiveScanner?

HiveScanner is a Claude Code plugin that brings your notifications to you instead of making you go to them.

Instead of context-switching between Slack, email, GitHub notifications, and your calendar, HiveScanner watches all of them in the background using lightweight Python workers. No LLM tokens are burned during idle polling. The Queen (the Claude-powered orchestrator) only wakes up when there's something worth showing you -- a PR that needs your review, a CI failure on your branch, a teammate who pinged you, or a meeting starting in ten minutes.

Everything is surfaced inline in your Claude Code session. Dismiss with a word, act immediately, or let it sit -- your workflow, your rules.

---

## Key Features

| Feature | Description |
|---|---|
| **Zero-token background polling** | Python workers handle all polling. The LLM is never invoked during idle cycles -- zero cost when nothing's happening. |
| **Bootstrap silence** | On first run, HiveScanner snapshots current state without flooding you with existing notifications. Only genuinely new items are surfaced. |
| **Watermark-based incremental polling** | Each scanner tracks a high-water mark. Only items newer than the last poll are fetched -- no duplicates, no re-processing. |
| **Smart batch grouping** | 5+ items from the same author are collapsed into a single summary instead of overwhelming your screen. |
| **Pollen lifecycle** | Each notification (pollen) flows through a clear lifecycle: `pending` -> `acknowledged` or `acted`. 7-day retention with automatic pruning. Pending pollen is never pruned. |
| **Triage autonomy with 6 safety gates** | Optional auto-response for oncall scenarios, gated by: autonomy toggle, draft existence, group allowlist, rate limiting, content safety (no remediation language), and attribution prefix enforcement. |
| **Community scanner ecosystem** | Extend HiveScanner with community-built scanners. Hire and fire them with `/hive hire <name>` and `/hive fire <name>`. Third-party scanners run sandboxed in subprocesses. |
| **Atomic file writes** | All state files (pollen, watermarks, config) are written atomically via tmp+rename -- crash-safe, no corruption. |

---

## Quick Start

### 1. Install

Add the HiveScanner marketplace and install the plugin:

```bash
claude plugin marketplace add github:dhruvil009/hivescanner
claude plugin install hivescanner
```

Or clone the repo and load it for a single session:

```bash
git clone https://github.com/dhruvil009/hivescanner.git ~/hivescanner
claude --plugin-dir ~/hivescanner
```

### 2. Start the setup wizard

```
/hive
```

HiveScanner launches an interactive wizard that walks you through configuration.

### 3. Configure your sources

The wizard will ask you to set up:

- **GitHub** -- which repos to watch, whether to track reviews, CI, and @mentions (requires `gh` CLI or `$GITHUB_TOKEN`)
- **Slack** -- optional; which channels and DMs to monitor (requires `$SLACK_TOKEN`)
- **Calendar** -- optional; Google Calendar integration for meeting prep and reminders
- **git_status** -- enabled by default; watches local directories for uncommitted changes, stale branches, merge conflicts, and forgotten stashes

### 4. Set your poll interval

Choose how frequently workers check for updates:

- Every 2 minutes (aggressive)
- **Every 5 minutes (recommended)**
- Every 10 minutes (relaxed)

### 5. You're done

HiveScanner starts running silently in the background. You'll see a status banner confirming your active scanners, then it disappears until something needs your attention.

```
Initializing HiveScanner. Deploying workers to the field...
Scanners: github (3 repos, reviews on, CI on)
          git_status (2 dirs)
Poll interval: 5 min
Pending pollen: 0 from last session
```

Use `/hive status` to check health anytime. Use `/hive stop` to shut down.

---

## How It Works

HiveScanner uses a **Queen / Worker / Pollen / Hive** architecture:

### The Queen (SKILL.md)

The session orchestrator. The Queen is a Claude Code skill (`/hive`) that manages the lifecycle -- starting scanners, classifying incoming pollen by relevance (HIGH / MEDIUM / LOW), surfacing what matters, and handling your natural-language responses ("got it", "dismiss all", "what did Jane post about?"). The Queen is only invoked when workers return with new data or when you interact. During idle polling, it sleeps -- zero tokens consumed.

### Workers (Python scanners)

Lightweight Python classes that poll external data sources. Each worker implements a `poll()` method that returns new items and an updated watermark. Workers run in the background via `scanner_loop.py`, which manages the poll-sleep-poll cycle, lock files, signal handling, and batch caps (20 items per cycle). Third-party community scanners run sandboxed in subprocesses for isolation.

### Pollen

Individual notifications or updates. Each pollen grain has an ID, source, type, relevance classification, preview text, and metadata. Pollen flows through a lifecycle:

```
discovered -> pending -> acknowledged (dismissed)
                      -> acted (you handled it externally)
```

Pending pollen persists across sessions. Acknowledged/acted pollen is retained for 7 days, then pruned. Pollen is deduplicated by ID -- you'll never see the same notification twice.

### The Hive

Persistent state on disk at `~/.hivescanner/`. Contains:

- `config.json` -- your scanner configuration and preferences
- `pollen.json` -- all pollen with lifecycle state
- `watermarks.json` -- per-scanner high-water marks for incremental polling
- `audit.json` -- triage action audit log
- `.lock` -- PID lockfile preventing duplicate scanner loops
- `scanners/` -- installed community scanner files

All state survives context compaction, session restarts, and crashes. The Queen re-reads from disk whenever it needs to.

### The polling loop

```
scanner_loop.py starts
    -> acquires lockfile
    -> loads config, watermarks, scanners
    -> LOOP:
        poll all enabled scanners (using watermarks for incremental fetch)
        check if user already acted on pending pollen externally
        if new pollen or acted IDs found:
            output JSON to stdout -> Queen wakes up
            break
        else:
            sleep(poll_interval)
    -> Queen classifies, surfaces, restarts the loop
```

---

## Built-in Scanners

| Scanner | What it watches | Pollen types |
|---|---|---|
| **github** | PR review requests, CI status (pass/fail), @mentions, issue assignments, general notifications | `review_needed`, `ci_failure`, `ci_passed`, `mention`, `issue_assigned`, `notification` |
| **git_status** | Uncommitted changes, branch behind remote, forgotten stashes, merge conflicts | `uncommitted_warning`, `branch_behind`, `stash_reminder`, `merge_conflict` |
| **calendar** | Upcoming events (30min + 10min reminders), new/changed events | `meeting_reminder`, `event_changed` |
| **gchat** | Google Chat DMs and @mentions in configured spaces | `gchat_dm`, `gchat_mention` |
| **whatsapp** | Incoming messages from configured chats | `whatsapp_message` |
| **email** | New emails, urgent VIP sender alerts via Gmail | `email_new`, `email_urgent` |
| **weather** | Daily morning briefing, significant temperature swing alerts | `weather_morning`, `weather_alert` |

Calendar, GChat, and Email require the [Google Workspace CLI](https://github.com/googleworkspace/cli) (`gws`). WhatsApp requires [whatsapp-cli](https://github.com/vicentereig/whatsapp-cli). Weather uses [wttr.in](https://wttr.in) (no install or API key needed).

---

## Why HiveScanner?

### The problem

Traditional developer notifications are scattered across a dozen surfaces. You check Slack. You check email. You check GitHub. You open a dashboard. Each context switch costs you focus. And most of the time, there's nothing new.

Tools like OpenClaw and NanoClaw tried to solve this by putting AI in your chat apps -- WhatsApp, Slack, Discord. But that just moves the problem. Your AI lives in one window, your code lives in another, and you're still context-switching.

### The flip

HiveScanner flips the script. Instead of AI living in your chat app, monitoring lives in your coding tool -- the place where you already spend your day. Instead of you going **to** your notifications, your notifications come **to you**.

| Traditional workflow | HiveScanner workflow |
|---|---|
| You open Slack to check for messages | DMs and mentions surface in your Claude Code session |
| You visit GitHub to see if CI passed | CI failures appear inline when they happen |
| You check email for review requests | Review requests show up with a suggested action |
| You open Google Calendar before meetings | Meeting reminders appear 30 and 10 minutes before |
| You forget about that stashed change | Stash reminders surface automatically |

### Zero cost when idle

Other monitoring agents burn LLM tokens on every polling cycle -- even when nothing has changed. OpenClaw runs a ReAct reasoning loop continuously. NanoClaw invokes the Agent SDK each cycle. Both rack up token costs while idling.

HiveScanner's Python workers handle all the polling deterministically. The LLM (the expensive part) is only invoked when new pollen actually arrives. If your repos are quiet, your calendar is clear, and nobody's pinged you -- HiveScanner costs exactly zero tokens.

### How HiveScanner compares

| Dimension | OpenClaw | NanoClaw | HiveScanner |
|---|---|---|---|
| **Interface** | Messaging apps | Messaging apps | Claude Code (IDE) |
| **Idle cost** | LLM tokens every cycle | Agent SDK tokens every cycle | Zero tokens |
| **Security** | Catastrophic (CVEs, malicious skills) | Container isolation | 6-gate safety + sandboxed scanners |
| **Extensibility** | ClawHub (compromised) | None | Community scanner ecosystem |
| **Architecture** | 500K LOC, 70+ deps | ~500 LOC TypeScript | Minimal Python pollers + plugin manifest |
| **Install** | Clone + configure gateway | Docker container | `git clone` -> `/hive` |

### Plugin-native, not another process

HiveScanner is a Claude Code plugin, not a standalone application. No separate gateway server. No port binding. No Docker container to manage. Type `/hive`, configure once, and it runs inside the tool you already have open. One less thing in your process manager.

---

## Community Scanners

HiveScanner is designed to be extended. The core ships with built-in scanners for GitHub and git status -- but the real power is the community scanner ecosystem. If a service has an API, you can build a scanner for it.

Community scanners are self-contained Python modules that plug into HiveScanner through a sandboxed JSON-over-stdio protocol. They run in isolated subprocesses, never touching the main process, so you can iterate fast without worrying about breaking anything.

**Already available:**

| Scanner | Description | Author |
|---------|-------------|--------|
| **Linear** | Monitors Linear issues and status changes | hivescanner-community |
| **RSS** | Monitors RSS/Atom feeds for new entries | hivescanner-community |
| **Slack** | Monitors Slack channels and DMs for messages, mentions, and thread replies | hivescanner-community |
| **Facebook** | Monitors Facebook page notifications and Messenger messages | hivescanner-community |
| **Twitter / X** | Monitors Twitter/X mentions and DMs | hivescanner-community |
| **PagerDuty** | Monitors PagerDuty incidents and triggered alerts | hivescanner-community |
| **Sentry** | Monitors Sentry issues and error spikes | hivescanner-community |
| **Jira** | Monitors Jira assigned/updated/mentioned issues | hivescanner-community |
| **GitLab** | Monitors GitLab MR reviews, CI failures, and mentions | hivescanner-community |
| **Notion** | Monitors Notion page updates and comments | hivescanner-community |
| **Telegram** | Monitors Telegram messages and mentions via Bot API | hivescanner-community |
| **Discord** | Monitors Discord DMs and mentions via Bot API | hivescanner-community |
| **HackerNews** | Monitors HN top stories by keyword and username mentions | hivescanner-community |
| **Package Tracking** | Parses shipping emails from Gmail for tracking updates | hivescanner-community |

Want to monitor Datadog alerts? Your internal deploy system? Build a scanner and contribute it back.

---

## Build Your Own Scanner

A community scanner is just a Python class with two methods. Here's everything you need.

### The Scanner Interface

```python
class YourScanner:
    name = "your-scanner"  # unique identifier

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        """Fetch new items since `watermark`. Return (pollen_list, new_watermark)."""
        ...

    def configure(self) -> dict:
        """Return default config template for this scanner."""
        ...
```

That's it. `poll` fetches new data and returns a list of pollen dicts plus an updated watermark (an ISO timestamp string used to track what you've already seen). `configure` returns sensible defaults.

### Minimal Example

Here's a complete scanner in ~30 lines -- an RSS feed monitor:

```python
"""RSS scanner -- minimal community scanner example."""

import hashlib
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


class RssScanner:
    name = "rss"

    def configure(self) -> dict:
        return {"enabled": False, "feeds": [], "max_items_per_feed": 5}

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pollen = []

        for feed_url in config.get("feeds", []):
            req = urllib.request.Request(feed_url, headers={"User-Agent": "HiveScanner/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                root = ET.fromstring(resp.read())

            for item in root.findall(".//item")[:config.get("max_items_per_feed", 5)]:
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                pub_date = item.findtext("pubDate") or ""

                if pub_date and pub_date <= watermark:
                    continue

                pollen.append({
                    "id": f"rss-{hashlib.sha256(f'{feed_url}:{title}'.encode()).hexdigest()[:12]}",
                    "source": "rss",
                    "type": "rss_item",
                    "title": title[:100],
                    "preview": title[:200],
                    "discovered_at": now,
                    "author": "",
                    "author_name": "",
                    "group": "RSS",
                    "url": link,
                    "metadata": {"feed_url": feed_url},
                })

        return pollen, now


# Required: sandboxed execution entry point
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = RssScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
```

Every pollen dict must include: `id`, `source`, `type`, `title`, `preview`, `discovered_at`, `author`, `author_name`, `group`, `url`, and `metadata`.

### The Manifest: `teammate.json`

Every community scanner needs a `teammate.json` manifest alongside the adapter:

```json
{
  "name": "rss",
  "display_name": "RSS Feeds",
  "version": "1.0.0",
  "description": "Monitors RSS/Atom feeds for new entries",
  "author": "hivescanner-community",
  "adapter_file": "adapter.py",
  "config_template": {
    "enabled": false,
    "feeds": [],
    "max_items_per_feed": 5
  },
  "requirements": {
    "cli_tools": []
  },
  "qpm_budget": 1
}
```

| Field | Description |
|-------|-------------|
| `name` | Unique scanner identifier (`[a-zA-Z0-9_-]` only) |
| `display_name` | Human-readable name shown in the UI |
| `version` | Semver version string |
| `description` | One-line description of what the scanner monitors |
| `author` | Your name or handle |
| `adapter_file` | Python file containing the Scanner class (usually `adapter.py`) |
| `config_template` | Default configuration merged into `config.json` on hire |
| `requirements.cli_tools` | CLI tools that must be installed (checked during hire) |
| `qpm_budget` | Queries-per-minute budget for rate limiting |

### Sandboxed Execution

Community scanners do **not** run inside the main HiveScanner process. Instead, they run as isolated subprocesses using a JSON-over-stdio protocol:

1. HiveScanner spawns your scanner: `python adapter.py --sandboxed`
2. It sends a JSON command on stdin: `{"command": "poll", "config": {...}, "watermark": "..."}`
3. Your scanner prints a JSON response to stdout: `{"pollen": [...], "watermark": "..."}`
4. The subprocess exits

This means your scanner code is fully isolated -- it cannot access HiveScanner internals, modify shared state, or interfere with other scanners. Each poll runs in a fresh process with a 30-second timeout.

### The Hire/Fire Lifecycle

Once your scanner is in the `community/` directory:

```bash
# Install and activate a scanner
/hive hire linear

# This copies adapter.py to ~/.hivescanner/scanners/
# Copies teammate.json to ~/.hivescanner/teammates/linear/
# Merges config_template into config.json

# Remove a scanner (config is backed up automatically)
/hive fire linear

# Re-hiring restores your previous configuration
/hive hire linear
```

### Testing Your Scanner Locally

You can test your scanner directly without installing it into HiveScanner:

```bash
# Test the configure command
echo '{"command": "configure"}' | python community/your-scanner/adapter.py --sandboxed

# Test polling
echo '{"command": "poll", "config": {"enabled": true, "your_option": "value"}, "watermark": "1970-01-01T00:00:00Z"}' | python community/your-scanner/adapter.py --sandboxed
```

The output should be valid JSON. If your scanner returns pollen, you'll see them in the response.

---

## Security Model

HiveScanner takes a defense-in-depth approach to running third-party code. Every layer is designed so that a misbehaving scanner cannot compromise the system.

### Process Isolation

Community scanners **never** run inside the main HiveScanner process. They execute in isolated subprocesses via `subprocess.run()` with a 30-second timeout. The only communication channel is JSON over stdin/stdout -- no shared memory, no imports, no direct function calls.

```
Main Process                    Subprocess (sandboxed)
     |                               |
     |-- stdin: JSON command ------->|
     |                               |-- runs poll()
     |<-- stdout: JSON response -----|
     |                               |-- exits
```

### Scanner Name Validation

Scanner names are validated against the pattern `^[a-zA-Z0-9_-]+$`. This prevents path traversal attacks -- a scanner named `../../etc` would be rejected before any file operations occur.

### Atomic File Writes

All file writes (config, pollen, watermarks, audit log) use the atomic write pattern: write to a `.tmp` file, then `os.replace()` to the final path. This means a crash or power loss mid-write can never corrupt your data.

### No Secrets in Pollen

API tokens and credentials stay in environment variables. Scanners reference them by env var name (e.g., `"api_key_env": "LINEAR_API_KEY"`) -- the actual secret is read at runtime via `os.environ.get()` and never persisted to pollen, config, or audit files. Only metadata flows through the pollen pipeline.

### Built-in Scanner Auth

Built-in scanners like GitHub use the `gh` CLI tool, which inherits your existing authentication. HiveScanner never handles, stores, or transmits your GitHub token directly.

### GraphQL Injection Prevention

Scanners that use GraphQL APIs (like the Linear scanner) use parameterized variables -- query parameters are passed as separate `variables`, never interpolated into the query string. This prevents injection attacks by design.

---

## Triage Autonomy

HiveScanner can optionally auto-post triage responses to your oncall channels. This is governed by a **6-gate safety system** -- every gate must pass before any content is posted. If any single gate fails, the post is blocked and logged.

### The 6 Gates

| Gate | Check | What It Prevents |
|------|-------|-------------------|
| **1. Global Kill Switch** | `autonomy.enabled` must be `true` in config | Accidental posts when autonomy is off |
| **2. Draft Exists** | Pollen must have a `triage_draft` in its metadata | Posting without prepared content |
| **3. Group Allowlist** | Target group must be in `oncall_groups` list | Posts to unauthorized channels |
| **4. Rate Limiting** | Max 3 posts per hour per group | Spam / runaway loops |
| **5. Content Safety** | Draft must not contain remediation language, code blocks, or suggestions | Dangerous automated advice |
| **6. Attribution Prefix** | Draft must start with `[Posted by HiveScanner - oncall triage assist]` | Unattributed automated posts |

### Template-Based Drafts

Triage responses are generated from **fixed templates** -- not LLMs, not AI-generated text. Templates contain structured prompts like "Can you share the crash ID?" or "What's the impact scope?" -- safe, predictable, and auditable.

### Content Safety Checks

Gate 5 uses regex patterns to block content containing:
- Remediation instructions ("try running...", "to fix this...")
- Code blocks (triple backticks)
- Suggestions or recommendations ("you should...", "I recommend...")
- Operational commands ("rollback", "revert", "hotfix")

If any pattern matches, the post is blocked. This ensures HiveScanner never gives operational advice autonomously.

### Full Audit Logging

Every triage action -- posted or blocked -- is logged to `~/.hivescanner/audit.json` with timestamps, gate results, and content previews. You always have a complete record of what happened and why.

### Instant Kill Switch

If anything goes wrong:

```bash
/hive autonomy off
```

This immediately sets `autonomy.enabled = false` in config. All 6 gates will fail at Gate 1 until you explicitly re-enable it. The toggle is logged to the audit trail.

---

## Contributing

We'd love your help expanding the scanner ecosystem. Here's how to contribute a new scanner:

1. **Fork the repo** and create a branch for your scanner
2. **Create your scanner directory**: `community/<your-scanner>/`
3. **Write `adapter.py`** with a class that implements `name`, `poll()`, and `configure()`
4. **Write `teammate.json`** with your scanner's manifest (see format above)
5. **Add the sandboxed execution block** at the bottom of your adapter
6. **Test locally** using the stdin/stdout protocol
7. **Submit a PR** with a description of what your scanner monitors

### Scanner Ideas (Contributions Welcome)

We're actively looking for community scanners for:

- **Datadog** -- monitor alerts and anomaly detection
- **Opsgenie** -- alert management
- **Custom internal tools** -- if it has an API, it can be a scanner

### Guidelines

- Keep your scanner self-contained -- use only Python stdlib when possible
- List any required CLI tools in `requirements.cli_tools`
- Never hardcode secrets -- use `api_key_env` to reference environment variables
- Handle errors gracefully -- return `([], watermark)` on failure so the watermark doesn't advance
- Include a descriptive `teammate.json` so users know what they're installing

---

## License

HiveScanner is licensed under **Commons Clause + Apache 2.0**. You are free to use, modify, and distribute the software. The Commons Clause restriction means it cannot be sold as a standalone commercial product or service. See the [LICENSE](LICENSE) file for full terms.
