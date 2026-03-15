# Sandboxed Execution

Community scanners do **not** run inside the main HiveScanner process. They use a JSON-over-stdio protocol for complete isolation.

## How It Works

1. HiveScanner spawns your scanner: `python adapter.py --sandboxed`
2. It sends a JSON command on stdin
3. Your scanner prints a JSON response to stdout
4. The subprocess exits

```
Main Process                    Subprocess (sandboxed)
     |                               |
     |── stdin: JSON command ───────>|
     |                               |── runs poll()
     |<── stdout: JSON response ─────|
     |                               |── exits
```

Each poll runs in a fresh process with a **30-second timeout**.

## Commands

### Poll

**Input:**

```json
{
  "command": "poll",
  "config": {
    "enabled": true,
    "token_env": "YOUR_TOKEN",
    "max_items": 20
  },
  "watermark": "2025-01-15T10:00:00Z"
}
```

**Output:**

```json
{
  "pollen": [
    {
      "id": "scanner-item-123",
      "source": "your-scanner",
      "type": "new_item",
      "title": "Something happened",
      "preview": "Details about what happened",
      "discovered_at": "2025-01-15T10:30:00Z",
      "author": "user",
      "author_name": "User Name",
      "group": "Group",
      "url": "https://example.com/item/123",
      "metadata": {}
    }
  ],
  "watermark": "2025-01-15T10:30:00Z"
}
```

### Configure

**Input:**

```json
{
  "command": "configure"
}
```

**Output:**

```json
{
  "config": {
    "enabled": false,
    "token_env": "YOUR_TOKEN",
    "max_items": 20
  }
}
```

## Entry Point Boilerplate

Add this at the bottom of your `adapter.py`:

```python
if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = json.loads(sys.stdin.read())
    scanner = YourScanner()
    if data["command"] == "poll":
        result_pollen, wm = scanner.poll(data["config"], data["watermark"])
        print(json.dumps({"pollen": result_pollen, "watermark": wm}))
    elif data["command"] == "configure":
        print(json.dumps({"config": scanner.configure()}))
```

## Isolation Guarantees

- **No shared memory** — each poll runs in a fresh process
- **No imports** — your scanner cannot import HiveScanner internals
- **No direct function calls** — communication is strictly JSON over stdio
- **30-second timeout** — runaway scanners are killed automatically
- **No filesystem access** — your scanner should only read env vars and make network requests
