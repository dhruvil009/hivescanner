# Sandboxed Execution

Community scanners run outside the main HiveScanner process through a strict JSON-over-stdio protocol.

## Runtime boundary

1. HiveScanner starts `python -I adapter.py --sandboxed`.
2. It sends one JSON command on stdin.
3. The adapter prints one JSON response on stdout.
4. The process exits.

Every poll has a 60-second wall timeout, a 1 MB output limit, a private temporary directory, an allowlisted environment, and best-effort POSIX CPU, address-space, file-size, descriptor, and process limits. A timeout kills the process group. JSON input and output must be UTF-8, finite, duplicate-key-free objects.

On macOS, HiveScanner also uses `sandbox-exec`: adapter/runtime paths are read-only, only the private temporary directory is writable, and outbound network access is allowed. On other platforms there is currently **no filesystem sandbox**. Process and resource controls reduce impact but do not make unreviewed code safe; review an adapter before enabling it.

Community manifests must declare `requirements.cli_tools` as `[]`. The constrained runtime does not support required child CLI dependencies.

## Commands

Poll input:

```json
{
  "command": "poll",
  "config": {"enabled": true, "token_env": "YOUR_TOKEN"},
  "watermark": "2026-07-15T10:00:00Z"
}
```

Poll output:

```json
{
  "pollen": [],
  "watermark": "2026-07-15T10:00:00Z"
}
```

Configure input and output:

```json
{"command": "configure"}
```

```json
{"config": {"enabled": false, "token_env": "YOUR_TOKEN"}}
```

## Dispatcher requirements

The dispatcher must reject duplicate keys, nonfinite numbers, non-object payloads, missing poll fields, and unknown commands. Copy the pattern from an existing v2 adapter such as `community/rss/adapter.py`; do not use a bare `json.loads(...); data["command"]` block as a production boundary.

## What the parent validates

The parent validates the response shape, pollen count, every pollen record, source-qualified IDs, bounded text/metadata/URLs, and watermark size before accepting anything. If any record is invalid, that scanner's watermark is preserved.
