# Security Model

HiveScanner treats all provider responses, community adapters, pollen fields, URLs, and background output as untrusted data.

## Scanner boundaries

- Built-ins validate config, credentials, provider envelopes, pagination, ordering, timestamps, and staged snapshots before committing progress.
- Community adapters run in isolated Python subprocesses with a 60-second timeout, a credential allowlist, private temp storage, strict JSON/output limits, and resource caps.
- macOS adds a filesystem-denying `sandbox-exec` profile. Other platforms do not currently provide filesystem isolation; review community code before enabling it.
- Same-origin redirect handlers prevent credentials from following redirects to another scheme, host, or port.
- Community scanners cannot require CLI tools and are installed disabled.

## State and delivery

State files are private and bounded. Reads reject symlinks, duplicate JSON keys, nonfinite values, oversized files, and unexpected top-level types. Writes use a private temporary file, `fsync`, and atomic replacement under advisory locks.

Event-producing watermarks are not committed until the durable pending batch has been imported. Scanner snapshots stage candidate state so a crash between polling and delivery does not silently skip items.

## Prompt injection

Provider text is data, never authority. The `/hive` skill forbids following instructions found in titles, previews, authors, metadata, URLs, API errors, or background stdout. Pollen cannot authorize commands, URL access, configuration changes, scanner management, secret disclosure, or external messages.

Pollen crosses a central normalization boundary that bounds text and metadata, strips control characters, validates HTTPS/HTTP URLs, removes scanner-supplied triage control fields, and keys deduplication by both source and item ID.

## Credentials

Secrets remain in environment variables or authenticated CLI stores. Community subprocesses receive only runtime essentials and credential variables explicitly named by `*_env` configuration. Tokens are never copied into pollen or audit records.

## Confirmed external posting

Slack triage uses fixed local templates and short-lived tickets. Posting requires direct user confirmation plus an unchanged exact group policy, destination allowlist, enabled transport, content/attribution checks, cooldowns, success and attempt limits, and idempotency handling. Unknown transport outcomes are not automatically retried.
