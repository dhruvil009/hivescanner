# Testing Locally

You can test your scanner directly without installing it into HiveScanner.

## Test the Configure Command

```bash
echo '{"command": "configure"}' | python community/your-scanner/adapter.py --sandboxed
```

Expected output — valid JSON with your default config:

```json
{
  "config": {
    "enabled": false,
    "token_env": "YOUR_TOKEN",
    "max_items": 20
  }
}
```

## Test Polling

```bash
echo '{"command": "poll", "config": {"enabled": true, "your_option": "value"}, "watermark": "1970-01-01T00:00:00Z"}' \
  | python community/your-scanner/adapter.py --sandboxed
```

Using `1970-01-01T00:00:00Z` as the watermark ensures all items are returned (useful for testing).

Expected output — valid JSON with pollen items:

```json
{
  "pollen": [
    {
      "id": "scanner-item-123",
      "source": "your-scanner",
      "type": "new_item",
      "title": "Something happened",
      "preview": "Details...",
      "discovered_at": "2025-01-15T10:30:00Z",
      "author": "user",
      "author_name": "User Name",
      "group": "Group",
      "url": "https://example.com",
      "metadata": {}
    }
  ],
  "watermark": "2025-01-15T10:30:00Z"
}
```

## Checklist

Before submitting your scanner, verify:

- [ ] `configure` command returns valid JSON
- [ ] `poll` command returns valid JSON with all required pollen fields
- [ ] Pollen IDs are unique and use a scanner-specific prefix
- [ ] Watermarks advance correctly (no duplicate items on subsequent polls)
- [ ] Errors are handled gracefully — return `([], watermark)` on failure
- [ ] No secrets are hardcoded — all tokens use env var references
- [ ] `teammate.json` is valid and all fields are populated
- [ ] Only Python stdlib is used (no pip dependencies)
