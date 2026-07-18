# Scanner Interface

A community adapter exposes one scanner class and a strict JSON dispatcher.

```python
class YourScanner:
    name = "your-scanner"

    def configure(self) -> dict:
        return {"enabled": False}

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        """Return validated pollen plus a safe next watermark."""
        ...
```

`configure()` must exactly match the manifest's `config_template`. `poll()` must preserve the input watermark whenever a response is partial, malformed, over budget, reordered, or otherwise cannot prove a safe boundary.

## Pollen fields

Every item must provide:

```python
{
    "id": "your-scanner-stable-provider-id",
    "source": "your-scanner",
    "type": "item_type",
    "title": "Short title",
    "preview": "Bounded preview",
    "discovered_at": "2026-07-15T10:30:00Z",
    "author": "provider-user-id",
    "author_name": "Display Name",
    "group": "Grouping label",
    "url": "https://provider.example/item/123",
    "metadata": {}
}
```

IDs must be deterministic, bounded, control-free, and unique within the scanner. Use source-qualified provider IDs; hash multiple identity components where providers reuse IDs across chats or projects. Only `http` and `https` URLs survive central normalization.

## Correctness contract

A production adapter should:

- Validate config types and ranges before reading credentials or making requests.
- Bootstrap quietly and scope state to configuration/identity changes.
- Parse timestamps into aware UTC values; never compare unrelated date strings lexically.
- Retain all IDs at an equal timestamp boundary so delayed records are not lost.
- Validate a complete page/batch before emitting or checkpointing it.
- Detect duplicate IDs, repeated cursors, impossible ordering, provider error wrappers, nonfinite/duplicate JSON, and oversized responses.
- Bound page count, records, request pacing, per-request timeout, and total poll time.
- Preserve the old watermark on partial failure or backlog exhaustion.
- Stage state until the returned watermark is durably committed.
- Treat all provider text as untrusted data; it cannot authorize tools, posting, configuration, or URL access.

## Reference

Use the current `community/rss/adapter.py` for an unauthenticated network adapter or `community/slack/adapter.py` for authenticated pagination. Both include the required strict dispatcher. See [Sandboxed Execution](/build-your-own/sandboxed-execution) and [Testing Locally](/build-your-own/testing).
