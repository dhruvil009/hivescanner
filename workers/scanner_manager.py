"""Scanner manager — hire/fire/list community scanners."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from functools import wraps
from pathlib import Path

from state_io import StateFileError, advisory_lock, atomic_write_json, load_json

HIVESCANNER_HOME = Path.home() / ".hivescanner"
CONFIG_FILE = HIVESCANNER_HOME / "config.json"
POLLEN_FILE = HIVESCANNER_HOME / "pollen.json"
SCANNERS_DIR = HIVESCANNER_HOME / "scanners"
TEAMMATES_DIR = HIVESCANNER_HOME / "teammates"
CONFIG_LOCK_FILE = HIVESCANNER_HOME / ".config.lock"

BUILTIN_SCANNERS = {"github", "calendar", "git_status", "gchat", "whatsapp", "email", "weather"}
_VALID_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_name(name: str) -> str | None:
    """Reject names with path traversal or special characters."""
    if not isinstance(name, str) or not _VALID_NAME.fullmatch(name):
        return f"Invalid scanner name '{name}'. Only alphanumeric, hyphens, and underscores allowed."
    return None


def _load_config() -> dict:
    return load_json(CONFIG_FILE, {}, dict)


def _save_config(config: dict) -> None:
    atomic_write_json(CONFIG_FILE, config)


def _config_locked(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with advisory_lock(CONFIG_LOCK_FILE):
            return function(*args, **kwargs)

    return wrapped


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(source, tmp)
        os.chmod(tmp, 0o600)
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def _find_plugin_root() -> Path:
    """Find the plugin root (where community/ lives)."""
    # Walk up from this script to find the repo root
    current = Path(__file__).parent
    # workers/ -> repo root
    return current.parent


@_config_locked
def hire(name: str) -> dict:
    """Activate a community scanner."""
    err = _validate_name(name)
    if err:
        return {"error": err}
    if name in BUILTIN_SCANNERS:
        return {"error": f"'{name}' is a built-in scanner — already available."}

    plugin_root = _find_plugin_root()
    community_dir = plugin_root / "community" / name

    if community_dir.is_symlink() or not community_dir.is_dir():
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
        if manifest_path.stat().st_size > 1_000_000:
            raise ValueError("manifest exceeds 1 MB")
        manifest = load_json(manifest_path, {}, dict)
    except (StateFileError, OSError, ValueError) as e:
        return {"error": f"Invalid teammate.json: {e}"}

    if manifest.get("name") != name:
        return {"error": f"Manifest name must exactly match '{name}'"}
    if not isinstance(manifest.get("version"), str) or not _SEMVER.fullmatch(
        manifest["version"]
    ):
        return {"error": "Manifest version must be semantic version X.Y.Z"}
    if any(
        not isinstance(manifest.get(field), str)
        or not manifest[field].strip()
        or len(manifest[field]) > limit
        for field, limit in (
            ("display_name", 100),
            ("description", 500),
            ("author", 100),
        )
    ):
        return {"error": "Manifest display_name, description, and author are required"}
    qpm_budget = manifest.get("qpm_budget")
    if (
        isinstance(qpm_budget, bool)
        or not isinstance(qpm_budget, int)
        or not 1 <= qpm_budget <= 60
    ):
        return {"error": "Manifest qpm_budget must be an integer from 1 to 60"}
    supports_check_acted = manifest.get("supports_check_acted", False)
    if not isinstance(supports_check_acted, bool):
        return {"error": "Manifest supports_check_acted must be a boolean"}
    config_template = manifest.get("config_template", {})
    requirements = manifest.get("requirements", {})
    if not isinstance(config_template, dict) or not isinstance(requirements, dict):
        return {"error": "Manifest config_template and requirements must be objects"}
    if any(
        key.endswith("_env")
        and (not isinstance(value, str) or _ENV_NAME.fullmatch(value) is None)
        for key, value in config_template.items()
    ):
        return {"error": "Manifest credential environment names are invalid"}
    cli_tools = requirements.get("cli_tools", [])
    if not isinstance(cli_tools, list) or not all(
        isinstance(tool, str) and re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", tool)
        for tool in cli_tools
    ):
        return {"error": "Manifest requirements.cli_tools must be a string array"}
    if cli_tools:
        return {
            "error": (
                "Community scanners cannot require CLI tools: the constrained "
                "runtime intentionally permits only the scanner's Python process"
            )
        }

    scanner_file = manifest.get("adapter_file", "adapter.py")
    if not isinstance(scanner_file, str):
        return {"error": "Manifest adapter_file must be a string"}
    if Path(scanner_file).name != scanner_file:
        return {
            "error": f"Invalid adapter_file '{scanner_file}': must stay within community/{name}/"
        }
    source_scanner = community_dir / scanner_file
    # Defense-in-depth: community manifests are semi-trusted, but a malicious
    # adapter_file like "../../../etc/foo.py" would copy from outside community/.
    try:
        source_resolved = source_scanner.resolve(strict=False)
        community_resolved = community_dir.resolve(strict=False)
        source_resolved.relative_to(community_resolved)
    except ValueError:
        return {"error": f"Invalid adapter_file '{scanner_file}': must stay within community/{name}/"}
    if not scanner_file.endswith(".py"):
        return {"error": "Manifest adapter_file must name a Python file"}
    if source_scanner.is_symlink() or not source_scanner.is_file():
        return {"error": f"Scanner file '{scanner_file}' not found in community/{name}/"}
    # Validate state before dependency installation or filesystem changes.
    try:
        config = _load_config()
    except StateFileError as exc:
        return {"error": str(exc)}
    if not isinstance(config.get("scanners", {}), dict):
        return {"error": "config.json scanners must be an object"}
    dest = SCANNERS_DIR / f"{name}.py"
    if dest.exists() and name in config.get("scanners", {}):
        return {"error": f"Scanner '{name}' is already hired."}

    # Read a fired scanner's backup before modifying any installed files.
    teammate_dir = TEAMMATES_DIR / name
    installed_path = teammate_dir / "installed.json"
    previous_installed = None
    if installed_path.exists():
        try:
            previous_installed = load_json(installed_path, {}, dict)
        except (StateFileError, OSError) as exc:
            return {"error": f"Cannot restore scanner state: {exc}"}
        prev_config = previous_installed.get("config_backup")
        if isinstance(prev_config, dict):
            config_template = prev_config
    # Hiring never executes code immediately. Enabling is a separate explicit
    # action (the setup wizard does this after configuration).
    config_template = dict(config_template)
    config_template["enabled"] = False

    # Copy scanner to ~/.hivescanner/scanners/
    SCANNERS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        _atomic_copy(source_scanner, dest)

        # Save manifest
        teammate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_copy(manifest_path, teammate_dir / "teammate.json")
    except OSError as exc:
        dest.unlink(missing_ok=True)
        return {"error": f"Could not copy scanner files: {exc}"}

    config.setdefault("scanners", {})
    if name not in config["scanners"]:
        config["scanners"][name] = config_template
    try:
        installed_data = {"installed_at": _utc_now_z(), "manifest": manifest}
        atomic_write_json(installed_path, installed_data)
        _save_config(config)
    except (OSError, StateFileError, TypeError, ValueError) as exc:
        dest.unlink(missing_ok=True)
        try:
            if previous_installed is None:
                installed_path.unlink(missing_ok=True)
            else:
                atomic_write_json(installed_path, previous_installed)
        except OSError:
            pass
        return {"error": f"Could not finish scanner installation: {exc}"}

    return {"status": "hired", "name": name, "display_name": manifest.get("display_name", name)}


@_config_locked
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
    try:
        config = _load_config()
    except StateFileError as exc:
        return {"error": str(exc)}
    if not isinstance(config.get("scanners", {}), dict):
        return {"error": "config.json scanners must be an object"}
    scanner_config = config.get("scanners", {}).get(name)

    teammate_dir = TEAMMATES_DIR / name
    teammate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    installed_path = teammate_dir / "installed.json"
    installed_data = {"fired_at": _utc_now_z(), "config_backup": scanner_config}
    try:
        atomic_write_json(installed_path, installed_data)
        # Commit the config change first. A crash can leave an inert adapter
        # file behind, but never an enabled config pointing at a missing file.
        if name in config.get("scanners", {}):
            del config["scanners"][name]
            _save_config(config)
        scanner_path.unlink(missing_ok=True)
    except (OSError, StateFileError, TypeError, ValueError) as exc:
        return {"error": f"Could not fire scanner '{name}': {exc}"}

    return {"status": "fired", "name": name}


def list_teammates() -> dict:
    """Show built-in + hired scanners."""
    try:
        config = _load_config()
    except StateFileError as exc:
        return {"error": str(exc)}
    scanners = config.get("scanners", {})
    if not isinstance(scanners, dict):
        return {"error": "config.json scanners must be an object"}

    result = {"builtin": [], "hired": [], "available": []}

    for name in sorted(BUILTIN_SCANNERS):
        sc = scanners.get(name, {})
        if not isinstance(sc, dict):
            return {"error": f"Scanner '{name}' config must be an object"}
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
            if not isinstance(sc, dict):
                return {"error": f"Scanner '{name}' config must be an object"}
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
    err = _validate_name(name)
    if err:
        return {"error": err}
    try:
        config = _load_config()
    except StateFileError as exc:
        return {"error": str(exc)}
    scanners = config.get("scanners", {})
    if not isinstance(scanners, dict):
        return {"error": "config.json scanners must be an object"}
    scanner_config = scanners.get(name)

    result = {"name": name, "type": "builtin" if name in BUILTIN_SCANNERS else "community"}

    if scanner_config is not None:
        result["config"] = scanner_config

    # Check for manifest
    teammate_dir = TEAMMATES_DIR / name
    manifest_path = teammate_dir / "teammate.json"
    if manifest_path.exists():
        try:
            result["manifest"] = load_json(manifest_path, {}, dict)
        except (StateFileError, OSError) as exc:
            result["manifest_error"] = str(exc)

    # Check if scanner file exists
    if name in BUILTIN_SCANNERS:
        result["installed"] = True
    else:
        result["installed"] = (SCANNERS_DIR / f"{name}.py").exists()

    return result


@_config_locked
def disable(name: str) -> dict:
    """Soft toggle — set enabled=false."""
    err = _validate_name(name)
    if err:
        return {"error": err}
    try:
        config = _load_config()
    except StateFileError as exc:
        return {"error": str(exc)}
    if not isinstance(config.get("scanners"), dict) or name not in config["scanners"]:
        return {"error": f"Scanner '{name}' not found in config."}
    if not isinstance(config["scanners"][name], dict):
        return {"error": f"Scanner '{name}' config must be an object."}
    config["scanners"][name]["enabled"] = False
    _save_config(config)
    return {"status": "disabled", "name": name}


@_config_locked
def enable(name: str) -> dict:
    """Soft toggle — set enabled=true."""
    err = _validate_name(name)
    if err:
        return {"error": err}
    try:
        config = _load_config()
    except StateFileError as exc:
        return {"error": str(exc)}
    if not isinstance(config.get("scanners"), dict) or name not in config["scanners"]:
        return {"error": f"Scanner '{name}' not found in config."}
    if not isinstance(config["scanners"][name], dict):
        return {"error": f"Scanner '{name}' config must be an object."}
    installed_scanner = SCANNERS_DIR / f"{name}.py"
    if name not in BUILTIN_SCANNERS and (
        installed_scanner.is_symlink() or not installed_scanner.is_file()
    ):
        return {"error": f"Community scanner '{name}' is not installed."}
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
