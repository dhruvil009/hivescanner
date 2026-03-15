# Architecture

HiveScanner uses a **Queen / Worker / Pollen / Hive** architecture.

## The Queen (SKILL.md)

The session orchestrator. The Queen is a Claude Code skill (`/hive`) that manages the lifecycle — starting scanners, classifying incoming pollen by relevance (HIGH / MEDIUM / LOW), surfacing what matters, and handling your natural-language responses.

The Queen is only invoked when workers return with new data or when you interact. During idle polling, it sleeps — **zero tokens consumed**.

## Workers (Python Scanners)

Lightweight Python classes that poll external data sources. Each worker implements a `poll()` method that returns new items and an updated watermark.

Workers run in the background via `scanner_loop.py`, which manages the poll-sleep-poll cycle, lock files, signal handling, and batch caps (20 items per cycle).

Community scanners run sandboxed in subprocesses for isolation.

## Pollen

Individual notifications or updates. Each pollen grain has an ID, source, type, relevance classification, preview text, and metadata.

See [Pollen Lifecycle](/concepts/pollen-lifecycle) for the full lifecycle flow.

## The Hive

Persistent state on disk at `~/.hivescanner/`. Contains:

| File | Purpose |
|------|---------|
| `config.json` | Scanner configuration and preferences |
| `pollen.json` | All pollen with lifecycle state |
| `watermarks.json` | Per-scanner high-water marks for incremental polling |
| `audit.json` | Triage action audit log |
| `.lock` | PID lockfile preventing duplicate scanner loops |
| `scanners/` | Installed community scanner files |
| `teammates/` | Community scanner manifests |

All state survives context compaction, session restarts, and crashes.

## The Polling Loop

```
scanner_loop.py starts
    → acquires lockfile
    → loads config, watermarks, scanners
    → LOOP:
        poll all enabled scanners (using watermarks)
        check if user acted on pending pollen externally
        if new pollen or acted IDs found:
            output JSON to stdout → Queen wakes up
            break
        else:
            sleep(poll_interval)
    → Queen classifies, surfaces, restarts the loop
```

## Zero-Token Design

The key insight: **Python workers handle all polling deterministically**. The LLM is only invoked when new pollen arrives. If your repos are quiet, your calendar is clear, and nobody's pinged you — HiveScanner costs exactly zero tokens.

| Component | Runs When | Token Cost |
|-----------|-----------|------------|
| Workers | Every poll cycle | Zero |
| Queen | New pollen arrives or user interacts | Minimal |
| Idle period | Between polls | Zero |
