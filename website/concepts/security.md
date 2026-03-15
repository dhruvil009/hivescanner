# Security Model

HiveScanner takes a defense-in-depth approach to running third-party code.

## Process Isolation

Community scanners **never** run inside the main HiveScanner process. They execute in isolated subprocesses via `subprocess.run()` with a 30-second timeout. The only communication channel is JSON over stdin/stdout — no shared memory, no imports, no direct function calls.

```
Main Process                    Subprocess (sandboxed)
     |                               |
     |── stdin: JSON command ───────>|
     |                               |── runs poll()
     |<── stdout: JSON response ─────|
     |                               |── exits
```

## Scanner Name Validation

Scanner names are validated against `^[a-zA-Z0-9_-]+$`. This prevents path traversal attacks — a scanner named `../../etc` would be rejected before any file operations occur.

## Atomic File Writes

All file writes (config, pollen, watermarks, audit log) use the atomic write pattern: write to a `.tmp` file, then `os.replace()` to the final path. A crash or power loss mid-write can never corrupt your data.

## No Secrets in Pollen

API tokens and credentials stay in environment variables. Scanners reference them by env var name (e.g., `"token_env": "LINEAR_API_KEY"`) — the actual secret is read at runtime via `os.environ.get()` and never persisted to pollen, config, or audit files.

## Built-in Scanner Auth

Built-in scanners like GitHub use the `gh` CLI, which inherits your existing authentication. HiveScanner never handles, stores, or transmits your GitHub token directly.

## GraphQL Injection Prevention

Scanners that use GraphQL APIs (like Linear) use parameterized variables — query parameters are passed as separate `variables`, never interpolated into the query string.
