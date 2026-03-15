"""Scanner manager — hire/fire/list community scanners."""

import json
import os
import re
import shutil
import sys
from pathlib import Path

HIVESCANNER_HOME = Path.home() / ".hivescanner"
CONFIG_FILE = HIVESCANNER_HOME / "config.json"
POLLEN_FILE = HIVESCANNER_HOME / "pollen.json"
SCANNERS_DIR = HIVESCANNER_HOME / "scanners"
TEAMMATES_DIR = HIVESCANNER_HOME / "teammates"

BUILTIN_SCANNERS = {"github", "calendar", "git_status", "gchat", "whatsapp", "email", "weather"}
_VALID_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_name(name: str) -> str | None:
    """Reject names with path traversal or special characters."""
    if not name or not _VALID_NAME.match(name):
        return f"Invalid scanner name '{name}'. Only alphanumeric, hyphens, and underscores allowed."
    return None


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_config(config: dict) -> None:
    HIVESCANNER_HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(str(tmp), str(CONFIG_FILE))


def _find_plugin_root() -> Path:
    """Find the plugin root (where community/ lives)."""
    # Walk up from this script to find the repo root
    current = Path(__file__).parent
    # workers/ -> repo root
    return current.parent


def hire(name: str) -> dict:
    """Activate a community scanner."""
    err = _validate_name(name)
    if err:
        return {"error": err}
    if name in BUILTIN_SCANNERS:
        return {"error": f"'{name}' is a built-in scanner — already available."}

    plugin_root = _find_plugin_root()
    community_dir = plugin_root / "community" / name

    if not community_dir.is_dir():
        available = []
        community_base = plugin_root / "community"
        if community_base.is_dir():
            available = [d.name for d in community_base.iterdir()
                        if d.is_dir() and not d.name.startswith(".")]
        return {"error": f"Community scanner '{name}' not found.",
                "available": available}

    # Load manifest
    manifest_path = community_dir / "teammate.json"
    if not manifest_path.exists():
        return {"error": f"No teammate.json found in community/{name}/"}
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        return {"error": f"Invalid teammate.json: {e}"}

    scanner_file = manifest.get("adapter_file", "adapter.py")
    source_scanner = community_dir / scanner_file
    if not source_scanner.exists():
        return {"error": f"Scanner file '{scanner_file}' not found in community/{name}/"}

    # Check CLI dependencies
    requirements = manifest.get("requirements", {})
    missing_tools = []
    for tool in requirements.get("cli_tools", []):
        if not shutil.which(tool):
            missing_tools.append(tool)
    if missing_tools:
        return {"error": f"Missing required CLI tools: {', '.join(missing_tools)}"}

    # Copy scanner to ~/.hivescanner/scanners/
    SCANNERS_DIR.mkdir(parents=True, exist_ok=True)
    dest = SCANNERS_DIR / f"{name}.py"
    shutil.copy2(str(source_scanner), str(dest))

    # Save manifest
    teammate_dir = TEAMMATES_DIR / name
    teammate_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(manifest_path), str(teammate_dir / "teammate.json"))

    # Merge config template
    config = _load_config()
    config_template = manifest.get("config_template", {})

    # Check for previous config backup (re-hire restores config)
    installed_path = teammate_dir / "installed.json"
    if installed_path.exists():
        try:
            installed = json.loads(installed_path.read_text())
            prev_config = installed.get("config_backup")
            if prev_config:
                config_template = prev_config
        except (json.JSONDecodeError, OSError):
            pass

    if "scanners" not in config:
        config["scanners"] = {}
    if name not in config["scanners"]:
        config["scanners"][name] = config_template
    _save_config(config)

    # Save install state
    installed_data = {"installed_at": _utc_now_z(), "manifest": manifest}
    with open(installed_path, "w") as f:
        json.dump(installed_data, f, indent=2)

    return {"status": "hired", "name": name, "display_name": manifest.get("display_name", name)}


def fire(name: str) -> dict:
    """Remove a community scanner."""
    err = _validate_name(name)
    if err:
        return {"error": err}
    if name in BUILTIN_SCANNERS:
        return {"error": f"Cannot fire built-in scanner '{name}'. Use 'disable' instead."}

    scanner_path = SCANNERS_DIR / f"{name}.py"
    if not scanner_path.exists():
        return {"error": f"Scanner '{name}' is not currently hired."}

    # Back up config before removal
    config = _load_config()
    scanner_config = config.get("scanners", {}).get(name)

    teammate_dir = TEAMMATES_DIR / name
    teammate_dir.mkdir(parents=True, exist_ok=True)
    installed_path = teammate_dir / "installed.json"
    installed_data = {"fired_at": _utc_now_z(), "config_backup": scanner_config}
    with open(installed_path, "w") as f:
        json.dump(installed_data, f, indent=2)

    # Remove scanner file
    scanner_path.unlink(missing_ok=True)

    # Remove from config scanners
    if name in config.get("scanners", {}):
        del config["scanners"][name]
        _save_config(config)

    # Clear pollen for this source
    if POLLEN_FILE.exists():
        try:
            hive = json.loads(POLLEN_FILE.read_text())
            hive["pollen"] = [p for p in hive.get("pollen", []) if p.get("source") != name]
            tmp = POLLEN_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(hive, f, indent=2)
            os.replace(str(tmp), str(POLLEN_FILE))
        except (json.JSONDecodeError, OSError):
            pass

    return {"status": "fired", "name": name}


def list_teammates() -> dict:
    """Show built-in + hired scanners."""
    config = _load_config()
    scanners = config.get("scanners", {})

    result = {"builtin": [], "hired": [], "available": []}

    for name in sorted(BUILTIN_SCANNERS):
        sc = scanners.get(name, {})
        result["builtin"].append({
            "name": name,
            "enabled": sc.get("enabled", False),
            "configured": name in scanners,
        })

    # Hired community scanners
    if SCANNERS_DIR.is_dir():
        for py_file in sorted(SCANNERS_DIR.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            name = py_file.stem
            sc = scanners.get(name, {})
            result["hired"].append({
                "name": name,
                "enabled": sc.get("enabled", False),
            })

    # Available but not hired
    plugin_root = _find_plugin_root()
    community_base = plugin_root / "community"
    if community_base.is_dir():
        hired_names = {h["name"] for h in result["hired"]}
        for d in sorted(community_base.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and d.name not in hired_names:
                result["available"].append(d.name)

    return result


def info(name: str) -> dict:
    """Show scanner details, config, manifest."""
    config = _load_config()
    scanner_config = config.get("scanners", {}).get(name)

    result = {"name": name, "type": "builtin" if name in BUILTIN_SCANNERS else "community"}

    if scanner_config:
        result["config"] = scanner_config

    # Check for manifest
    teammate_dir = TEAMMATES_DIR / name
    manifest_path = teammate_dir / "teammate.json"
    if manifest_path.exists():
        try:
            result["manifest"] = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Check if scanner file exists
    if name in BUILTIN_SCANNERS:
        result["installed"] = True
    else:
        result["installed"] = (SCANNERS_DIR / f"{name}.py").exists()

    return result


def disable(name: str) -> dict:
    """Soft toggle — set enabled=false."""
    config = _load_config()
    if name not in config.get("scanners", {}):
        return {"error": f"Scanner '{name}' not found in config."}
    config["scanners"][name]["enabled"] = False
    _save_config(config)
    return {"status": "disabled", "name": name}


def enable(name: str) -> dict:
    """Soft toggle — set enabled=true."""
    config = _load_config()
    if name not in config.get("scanners", {}):
        return {"error": f"Scanner '{name}' not found in config."}
    config["scanners"][name]["enabled"] = True
    _save_config(config)
    return {"status": "enabled", "name": name}


def _utc_now_z() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- CLI interface ---

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: scanner_manager.py <command> [args]"}))
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "hire":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "hire requires scanner name"}))
            sys.exit(1)
        print(json.dumps(hire(sys.argv[2]), indent=2))

    elif cmd == "fire":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "fire requires scanner name"}))
            sys.exit(1)
        print(json.dumps(fire(sys.argv[2]), indent=2))

    elif cmd == "list":
        print(json.dumps(list_teammates(), indent=2))

    elif cmd == "info":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "info requires scanner name"}))
            sys.exit(1)
        print(json.dumps(info(sys.argv[2]), indent=2))

    elif cmd == "disable":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "disable requires scanner name"}))
            sys.exit(1)
        print(json.dumps(disable(sys.argv[2]), indent=2))

    elif cmd == "enable":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "enable requires scanner name"}))
            sys.exit(1)
        print(json.dumps(enable(sys.argv[2]), indent=2))

    else:
        print(json.dumps({"error": f"Unknown command: {cmd}"}))
        sys.exit(1)
