"""Background scanner loop — polls data sources, exits when new pollen found."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

from dep_installer import preflight

HIVESCANNER_HOME = Path.home() / ".hivescanner"
LOCK_FILE = HIVESCANNER_HOME / ".lock"
WATERMARKS_FILE = HIVESCANNER_HOME / "watermarks.json"
POLLEN_FILE = HIVESCANNER_HOME / "pollen.json"
CONFIG_FILE = HIVESCANNER_HOME / "config.json"
WORKERS_DIR = Path(__file__).parent
THIRD_PARTY_DIR = HIVESCANNER_HOME / "scanners"

MAX_POLLEN_PER_CYCLE = 20
DEFAULT_POLL_INTERVAL = 300

_shutdown_requested = False
_META_LOCK_FD = None


def handle_signal(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)
if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, handle_signal)


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def output_pollen(pollen: list[dict], acted_ids: list[str]) -> None:
    print(json.dumps({
        "type": "new_pollen",
        "count": len(pollen),
        "timestamp": _utc_now_z(),
        "pollen": pollen,
        "acted_ids": acted_ids,
    }))


def output_error(msg: str) -> None:
    print(json.dumps({"type": "error", "message": msg, "timestamp": _utc_now_z()}))


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            if ok:
                return exit_code.value == STILL_ACTIVE
            return True
        return kernel32.GetLastError() == ERROR_ACCESS_DENIED
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_lock() -> None:
    # POSIX uses fcntl.flock; Windows uses the O_EXCL + PID check path.
    global _META_LOCK_FD
    HIVESCANNER_HOME.mkdir(parents=True, exist_ok=True)

    if fcntl is None:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            try:
                pid = int(LOCK_FILE.read_text().strip())
            except (ValueError, OSError):
                pid = -1
            if is_pid_running(pid):
                output_error(f"Another scanner loop running (PID {pid})")
                sys.exit(1)
            LOCK_FILE.unlink(missing_ok=True)
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        return

    meta = str(LOCK_FILE) + ".flock"
    _META_LOCK_FD = os.open(meta, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(_META_LOCK_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            pid = int(LOCK_FILE.read_text().strip())
        except (ValueError, OSError):
            pid = -1
        output_error(f"Another scanner loop running (PID {pid})")
        os.close(_META_LOCK_FD)
        _META_LOCK_FD = None
        sys.exit(1)

    LOCK_FILE.write_text(str(os.getpid()))


def release_lock() -> None:
    global _META_LOCK_FD
    LOCK_FILE.unlink(missing_ok=True)
    if _META_LOCK_FD is not None:
        if fcntl is not None:
            try:
                fcntl.flock(_META_LOCK_FD, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(_META_LOCK_FD)
        except OSError:
            pass
        _META_LOCK_FD = None


def load_watermarks() -> dict:
    if not WATERMARKS_FILE.exists():
        return {}
    try:
        return json.loads(WATERMARKS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_watermarks(watermarks: dict) -> None:
    HIVESCANNER_HOME.mkdir(parents=True, exist_ok=True)
    tmp = WATERMARKS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(watermarks, f, indent=2)
    os.replace(str(tmp), str(WATERMARKS_FILE))


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        output_error("No config.json found. Run /hive to set up.")
        sys.exit(1)
    try:
        config = json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        output_error(f"Failed to read config.json: {e}")
        sys.exit(1)

    username = config.get("user", {}).get("username", "")
    if not username or username == "YOUR_USERNAME":
        output_error("config.json has placeholder user.username — run /hive to configure.")
        sys.exit(1)

    poll_interval = config.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL)
    if poll_interval < 60:
        output_error(f"poll_interval_seconds ({poll_interval}) too low. Minimum 60.")
        sys.exit(1)

    return config


def load_pollen_ids() -> set[str]:
    """Load ALL pollen IDs from pollen.json — prevents re-reporting."""
    if not POLLEN_FILE.exists():
        return set()
    try:
        hive = json.loads(POLLEN_FILE.read_text())
        return {p["id"] for p in hive.get("pollen", []) if p.get("id")}
    except (json.JSONDecodeError, OSError):
        return set()


def _scan_scanner_dir(directory: Path, label: str = "") -> dict:
    """Scan directory for *Scanner classes in .py files (not starting with _)."""
    scanners = {}
    if not directory.is_dir():
        return scanners
    # Add workers/ (parent) to sys.path for imports like snapshot_store, dep_installer.
    # Do NOT add sources/ itself — files like email.py and calendar.py shadow stdlib modules.
    parent_str = str(directory.parent)
    if parent_str not in sys.path:
        sys.path.insert(0, parent_str)
    for py_file in sorted(directory.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"hivescanner_{label}_{py_file.stem}" if label else py_file.stem
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if (isinstance(obj, type)
                        and attr_name.endswith("Scanner")
                        and hasattr(obj, "name")
                        and hasattr(obj, "poll")):
                    instance = obj()
                    scanners[instance.name] = instance
        except Exception as e:
            print(f"[scanner] Failed to load {py_file.name} ({label}): {e}", file=sys.stderr)
    return scanners


def _poll_sandboxed(scanner_path: Path, config: dict, watermark: str) -> tuple[list, str]:
    """Run a 3rd-party scanner in a subprocess. JSON-over-stdio protocol."""
    input_data = json.dumps({"command": "poll", "config": config, "watermark": watermark})
    result = subprocess.run(
        [sys.executable, str(scanner_path), "--sandboxed"],
        input=input_data,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Scanner failed: {result.stderr[:200]}")
    output = json.loads(result.stdout)
    return output.get("pollen", output.get("items", [])), output.get("watermark", watermark)


def _check_acted_sandboxed(scanner_path: Path, pollen: dict, config: dict) -> bool:
    """Run a 3rd-party scanner's check_acted in subprocess."""
    input_data = json.dumps({"command": "check_acted", "item": pollen, "config": config})
    try:
        result = subprocess.run(
            [sys.executable, str(scanner_path), "--sandboxed"],
            input=input_data,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return False
        output = json.loads(result.stdout)
        return output.get("acted", False)
    except Exception:
        return False


def load_scanners() -> dict:
    """1st-party first, 3rd-party can override by name."""
    scanners = {}
    scanners.update(_scan_scanner_dir(WORKERS_DIR / "sources", "builtin"))
    # 3rd-party scanners are tracked separately for sandboxed execution
    return scanners


def get_third_party_scanners() -> dict[str, Path]:
    """Get paths to 3rd-party scanner files (for sandboxed execution)."""
    result = {}
    if not THIRD_PARTY_DIR.is_dir():
        return result
    for py_file in sorted(THIRD_PARTY_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        name = py_file.stem
        result[name] = py_file
    return result


def check_acted_pollen(config: dict, scanners: dict, third_party: dict[str, Path]) -> list[str]:
    """For each pending pollen, call the scanner's check_acted().
    Returns list of pollen IDs where user acted externally."""
    acted_ids = []
    if not POLLEN_FILE.exists():
        return acted_ids
    try:
        hive_data = json.loads(POLLEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return acted_ids

    username = config.get("user", {}).get("username", "")

    for p in hive_data.get("pollen", []):
        if p.get("status") != "pending":
            continue
        source = p.get("source", "")
        scanner_config = dict(config.get("scanners", {}).get(source, {}))
        scanner_config["_username"] = username

        if source in scanners:
            scanner = scanners[source]
            if not hasattr(scanner, "check_acted"):
                continue
            try:
                if scanner.check_acted(p, scanner_config):
                    acted_ids.append(p["id"])
            except Exception as e:
                print(f"[scanner] check_acted error for {source}: {e}", file=sys.stderr)
        elif source in third_party:
            if _check_acted_sandboxed(third_party[source], p, scanner_config):
                acted_ids.append(p["id"])

    return acted_ids


def poll_all(config: dict, scanners: dict, third_party: dict[str, Path], watermarks: dict) -> tuple[list, list]:
    """Poll all enabled scanners. Returns (pollen, acted_ids)."""
    tagged_pollen: list[tuple[str, dict]] = []
    new_watermarks: dict[str, str] = {}
    contributed_counts: dict[str, int] = {}

    for scanner_name, scanner_config in config.get("scanners", {}).items():
        if not scanner_config.get("enabled"):
            continue

        watermark = watermarks.get(scanner_name, "1970-01-01T00:00:00Z")

        pollen = None
        new_wm = None
        if scanner_name in scanners:
            try:
                pollen, new_wm = scanners[scanner_name].poll(scanner_config, watermark)
            except Exception as e:
                print(f"[scanner] Error polling {scanner_name}: {e}", file=sys.stderr)
                continue
        elif scanner_name in third_party:
            try:
                pollen, new_wm = _poll_sandboxed(third_party[scanner_name], scanner_config, watermark)
            except Exception as e:
                print(f"[scanner] Error polling sandboxed {scanner_name}: {e}", file=sys.stderr)
                continue
        else:
            continue

        new_watermarks[scanner_name] = new_wm
        contributed_counts[scanner_name] = len(pollen)
        for item in pollen:
            tagged_pollen.append((scanner_name, item))

    if len(tagged_pollen) > MAX_POLLEN_PER_CYCLE:
        # Round-robin interleave by scanner so no single scanner hogs the cap.
        # Preserves per-scanner order; scanners with fewer items get served fully.
        by_scanner: dict[str, list[dict]] = {}
        order: list[str] = []
        for scanner_name, item in tagged_pollen:
            if scanner_name not in by_scanner:
                order.append(scanner_name)
                by_scanner[scanner_name] = []
            by_scanner[scanner_name].append(item)

        interleaved: list[tuple[str, dict]] = []
        while len(interleaved) < MAX_POLLEN_PER_CYCLE:
            drained = True
            for name in order:
                bucket = by_scanner[name]
                if bucket:
                    interleaved.append((name, bucket.pop(0)))
                    drained = False
                    if len(interleaved) >= MAX_POLLEN_PER_CYCLE:
                        break
            if drained:
                break

        tagged_pollen = interleaved

    kept_counts: dict[str, int] = {}
    for scanner_name, _ in tagged_pollen:
        kept_counts[scanner_name] = kept_counts.get(scanner_name, 0) + 1

    for scanner_name, new_wm in new_watermarks.items():
        # Only advance watermark when all of this scanner's items survived the cap.
        if kept_counts.get(scanner_name, 0) == contributed_counts.get(scanner_name, 0):
            watermarks[scanner_name] = new_wm

    all_pollen = [item for _, item in tagged_pollen]

    acted_ids = check_acted_pollen(config, scanners, third_party)
    return all_pollen, acted_ids


def main():
    acquire_lock()
    try:
        config = load_config()
        preflight(config)  # Auto-install missing CLI deps for enabled scanners
        watermarks = load_watermarks()
        scanners = load_scanners()
        third_party = get_third_party_scanners()
        poll_interval = config.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL)

        while True:
            if _shutdown_requested:
                save_watermarks(watermarks)
                break

            pollen, acted_ids = poll_all(config, scanners, third_party, watermarks)

            if pollen:
                known_ids = load_pollen_ids()
                new_pollen = []
                for p in pollen:
                    pid = p.get("id")
                    if not pid:
                        print(f"[scanner] dropping pollen without id from source={p.get('source', 'unknown')}", file=sys.stderr)
                        continue
                    if pid in known_ids:
                        continue
                    new_pollen.append(p)
            else:
                new_pollen = []

            if new_pollen or acted_ids:
                save_watermarks(watermarks)
                output_pollen(new_pollen, acted_ids)
                break

            time.sleep(poll_interval)

    except Exception as e:
        output_error(str(e))
        sys.exit(1)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
