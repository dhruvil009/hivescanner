"""Background scanner loop — polls data sources, exits when new pollen found."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

from dep_installer import preflight
from pollen_schema import normalize_pollen, pollen_key
from state_io import atomic_write_json, load_json

HIVESCANNER_HOME = Path.home() / ".hivescanner"
LOCK_FILE = HIVESCANNER_HOME / ".lock"
WATERMARKS_FILE = HIVESCANNER_HOME / "watermarks.json"
POLLEN_FILE = HIVESCANNER_HOME / "pollen.json"
CONFIG_FILE = HIVESCANNER_HOME / "config.json"
PENDING_BATCH_FILE = HIVESCANNER_HOME / "pending_batch.json"
ACTED_CURSOR_FILE = HIVESCANNER_HOME / "acted_cursor.json"
WORKERS_DIR = Path(__file__).parent
THIRD_PARTY_DIR = HIVESCANNER_HOME / "scanners"
TEAMMATES_DIR = HIVESCANNER_HOME / "teammates"

MAX_POLLEN_PER_CYCLE = 20
DEFAULT_POLL_INTERVAL = 300
MAX_CONFIG_BYTES = 1_000_000
MAX_SANDBOX_OUTPUT_BYTES = 1_000_000
MAX_WATERMARK_BYTES = 256_000
MAX_WATERMARK_FILE_BYTES = 4_000_000
MAX_PENDING_BATCH_BYTES = 64_000_000
MAX_ITEMS_PER_SCANNER = 10_000
MAX_QUEUE_ITEMS = 10_000
MAX_NEW_ITEMS_PER_RUN = 5_000
MAX_ACTED_CHECKS_PER_RUN = 20
COMMUNITY_POLL_TIMEOUT_SECONDS = 60
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCANNER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

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


def output_pollen(pollen: list[dict], acted_ids: list[object]) -> None:
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
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_lock() -> None:
    # POSIX uses fcntl.flock; Windows uses the O_EXCL + PID check path.
    global _META_LOCK_FD
    HIVESCANNER_HOME.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(HIVESCANNER_HOME, 0o700)
    except OSError:
        pass

    if fcntl is None:
        try:
            fd = os.open(
                str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
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
            fd = os.open(
                str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        return

    meta = str(LOCK_FILE) + ".flock"
    _META_LOCK_FD = os.open(meta, os.O_CREAT | os.O_RDWR, 0o600)
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

    fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)


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
    watermarks = load_json(
        WATERMARKS_FILE,
        {},
        dict,
        max_bytes=MAX_WATERMARK_FILE_BYTES,
    )
    if len(watermarks) > 1_000 or any(
        not isinstance(name, str)
        or _SCANNER_NAME_RE.fullmatch(name) is None
        or not isinstance(value, str)
        or len(value.encode("utf-8", errors="replace")) > MAX_WATERMARK_BYTES
        for name, value in watermarks.items()
    ):
        raise RuntimeError(
            f"Cannot read {WATERMARKS_FILE}: invalid scanner name or watermark value; "
            "file was left untouched"
        )
    return watermarks


def save_watermarks(watermarks: dict) -> None:
    atomic_write_json(WATERMARKS_FILE, watermarks)


def load_pending_batch() -> dict | None:
    """Load a durable worker-to-Queen handoff, if one is awaiting persistence."""
    if not PENDING_BATCH_FILE.exists():
        return None
    batch = load_json(
        PENDING_BATCH_FILE,
        {},
        dict,
        max_bytes=MAX_PENDING_BATCH_BYTES,
    )
    if batch.get("schema_version") != 1:
        raise RuntimeError(f"Cannot read {PENDING_BATCH_FILE}: unsupported schema")
    if not isinstance(batch.get("pollen", []), list):
        raise RuntimeError(f"Cannot read {PENDING_BATCH_FILE}: 'pollen' must be a list")
    if not isinstance(batch.get("remaining_pollen", []), list):
        raise RuntimeError(
            f"Cannot read {PENDING_BATCH_FILE}: 'remaining_pollen' must be a list"
        )
    if not isinstance(batch.get("acted_ids", []), list):
        raise RuntimeError(f"Cannot read {PENDING_BATCH_FILE}: 'acted_ids' must be a list")
    if not isinstance(batch.get("watermarks", {}), dict):
        raise RuntimeError(f"Cannot read {PENDING_BATCH_FILE}: 'watermarks' must be an object")
    if (
        len(batch.get("pollen", [])) > MAX_POLLEN_PER_CYCLE
        or len(batch.get("remaining_pollen", [])) > MAX_NEW_ITEMS_PER_RUN
        or len(batch.get("acted_ids", [])) > MAX_ACTED_CHECKS_PER_RUN
        or any(
            not isinstance(name, str)
            or _SCANNER_NAME_RE.fullmatch(name) is None
            or not isinstance(value, str)
            or len(value.encode("utf-8", errors="replace")) > MAX_WATERMARK_BYTES
            for name, value in batch.get("watermarks", {}).items()
        )
    ):
        raise RuntimeError(f"Cannot read {PENDING_BATCH_FILE}: batch exceeds limits")
    return batch


def save_pending_batch(
    pollen: list[dict], acted_ids: list[object], watermarks: dict
) -> tuple[list[dict], list[object]]:
    """Persist the complete batch and return the first bounded delivery chunk."""
    delivery = pollen[:MAX_POLLEN_PER_CYCLE]
    remaining = pollen[MAX_POLLEN_PER_CYCLE:]
    atomic_write_json(PENDING_BATCH_FILE, {
        "schema_version": 1,
        "created_at": _utc_now_z(),
        "pollen": delivery,
        "remaining_pollen": remaining,
        "acted_ids": acted_ids,
        "watermarks": watermarks,
    })
    return delivery, acted_ids


def reconcile_pending_batch(watermarks: dict) -> tuple[list[dict], list[object]] | None:
    """Commit a delivered batch, or return it for safe redelivery.

    The scanner never commits event-producing watermarks until every pollen item
    from that batch exists in pollen.json. This closes the crash window between
    stdout delivery and the Queen's separate ``add_pollen`` command.
    """
    batch = load_pending_batch()
    if batch is None:
        return None

    known_keys = load_pollen_keys()
    pending_items = []
    for raw_item in batch.get("pollen", []):
        item = normalize_pollen(raw_item)
        if item is None:
            raise RuntimeError(f"Invalid pollen stored in {PENDING_BATCH_FILE}")
        if pollen_key(item["source"], item["id"]) not in known_keys:
            pending_items.append(item)

    acted_ids = batch.get("acted_ids", [])
    hive = load_json(POLLEN_FILE, {"pollen": []}, dict)
    still_pending_keys = {
        pollen_key(item.get("source"), item.get("id"))
        for item in hive.get("pollen", [])
        if isinstance(item, dict) and item.get("status") == "pending" and item.get("id")
    }
    pending_acted_ids = []
    for reference in acted_ids:
        if isinstance(reference, dict):
            if pollen_key(reference.get("source"), reference.get("id")) in still_pending_keys:
                pending_acted_ids.append(reference)
        elif isinstance(reference, str):
            # Compatibility with batches created before source-qualified acted
            # references were introduced.
            if any(key.endswith(f"\0{reference}") for key in still_pending_keys):
                pending_acted_ids.append(reference)

    if pending_items or pending_acted_ids:
        return pending_items, pending_acted_ids

    remaining = []
    for raw_item in batch.get("remaining_pollen", []):
        item = normalize_pollen(raw_item)
        if item is None:
            raise RuntimeError(f"Invalid overflow pollen stored in {PENDING_BATCH_FILE}")
        # An operator may have independently imported an item while it waited.
        if pollen_key(item["source"], item["id"]) not in known_keys:
            remaining.append(item)

    if remaining:
        delivery = remaining[:MAX_POLLEN_PER_CYCLE]
        atomic_write_json(PENDING_BATCH_FILE, {
            "schema_version": 1,
            "created_at": batch.get("created_at", _utc_now_z()),
            "pollen": delivery,
            "remaining_pollen": remaining[MAX_POLLEN_PER_CYCLE:],
            "acted_ids": [],
            "watermarks": batch.get("watermarks", {}),
        })
        return delivery, []

    for name, value in batch.get("watermarks", {}).items():
        if isinstance(name, str) and isinstance(value, str):
            watermarks[name] = value
    save_watermarks(watermarks)
    PENDING_BATCH_FILE.unlink(missing_ok=True)
    return None


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        output_error("No config.json found. Run /hive to set up.")
        sys.exit(1)
    try:
        if CONFIG_FILE.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError(f"file exceeds {MAX_CONFIG_BYTES} bytes")
        config = load_json(CONFIG_FILE, {}, dict)
    except (OSError, RuntimeError, ValueError) as e:
        output_error(f"Failed to read config.json: {e}")
        sys.exit(1)

    user_config = config.get("user")
    if not isinstance(user_config, dict):
        output_error("config.json user must be an object")
        sys.exit(1)
    username = user_config.get("username", "")
    if not isinstance(username, str) or not username.strip() or username == "YOUR_USERNAME":
        output_error("config.json has placeholder user.username — run /hive to configure.")
        sys.exit(1)

    poll_interval = config.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL)
    if (isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or not math.isfinite(poll_interval)):
        output_error("poll_interval_seconds must be a finite number")
        sys.exit(1)
    if poll_interval < 60 or poll_interval > 86400:
        output_error(
            f"poll_interval_seconds ({poll_interval}) outside supported range 60..86400"
        )
        sys.exit(1)

    scanners_config = config.get("scanners", {})
    if not isinstance(scanners_config, dict):
        output_error("config.json scanners must be an object")
        sys.exit(1)
    if any(not isinstance(value, dict) for value in scanners_config.values()):
        output_error("Every scanner config must be an object")
        sys.exit(1)
    if any(
        "enabled" in value and not isinstance(value["enabled"], bool)
        for value in scanners_config.values()
    ):
        output_error("Every scanner enabled flag must be a boolean")
        sys.exit(1)
    if any(not _SCANNER_NAME_RE.fullmatch(str(name)) for name in scanners_config):
        output_error("Scanner names may contain only letters, numbers, underscores, and hyphens")
        sys.exit(1)

    return config


def load_pollen_keys() -> set[str]:
    """Load source-qualified pollen IDs from pollen.json."""
    hive = load_json(POLLEN_FILE, {"pollen": []}, dict)
    pollen = hive.get("pollen", [])
    if not isinstance(pollen, list):
        raise RuntimeError(f"Cannot read {POLLEN_FILE}: 'pollen' must be a list")
    return {
        pollen_key(item.get("source"), item.get("id"))
        for item in pollen
        if isinstance(item, dict) and item.get("id")
    }


def pollen_queue_count() -> int:
    hive = load_json(POLLEN_FILE, {"pollen": []}, dict)
    pollen = hive.get("pollen", [])
    if not isinstance(pollen, list):
        raise RuntimeError(f"Cannot read {POLLEN_FILE}: 'pollen' must be a list")
    return len(pollen)


def load_pollen_ids() -> set[str]:
    """Compatibility helper returning unqualified pollen IDs."""
    hive = load_json(POLLEN_FILE, {"pollen": []}, dict)
    return {
        item["id"]
        for item in hive.get("pollen", [])
        if isinstance(item, dict) and item.get("id")
    }


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
                    if instance.name in scanners:
                        print(
                            f"[scanner] duplicate scanner name '{instance.name}' in {py_file.name}; "
                            "keeping the first implementation",
                            file=sys.stderr,
                        )
                        continue
                    scanners[instance.name] = instance
        except Exception as e:
            print(f"[scanner] Failed to load {py_file.name} ({label}): {e}", file=sys.stderr)
    return scanners


def _sandbox_environment(config: dict) -> dict[str, str]:
    """Pass only runtime essentials and explicitly configured credential vars."""
    allowed_names = {
        "PATH", "LANG", "LC_ALL", "LC_CTYPE",
        "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    }
    for key, value in config.items():
        if (
            key.endswith("_env")
            and isinstance(value, str)
            and _ENV_NAME_RE.fullmatch(value)
            and not value.startswith(("LD_", "DYLD_", "PYTHON"))
        ):
            allowed_names.add(value)
    return {name: os.environ[name] for name in allowed_names if name in os.environ}


def _sandbox_limits():
    """Best-effort POSIX resource limits for community scanner processes."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        address_space = 256 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_SANDBOX_OUTPUT_BYTES, MAX_SANDBOX_OUTPUT_BYTES))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))
    except (ImportError, OSError, ValueError):
        pass


def _sandbox_command(scanner_path: Path, temp_root: Path | None = None) -> list[str]:
    """Build a platform sandbox command when a local isolation facility exists."""
    python_command = [sys.executable, "-I", str(scanner_path), "--sandboxed"]
    sandbox_exec = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not sandbox_exec.exists():
        return python_command

    def quoted(path: str | Path) -> str:
        # sandbox-exec profiles use Scheme strings. JSON escaping is compatible
        # for quotes, backslashes, and control characters; hand-built escaping
        # would let a hostile TMPDIR inject a new profile form via a newline.
        return json.dumps(str(Path(path).resolve()))

    readable = {
        "/System",
        "/usr/lib",
        "/usr/share",
        "/private/etc",
        "/etc",
        "/opt/homebrew",
        str(scanner_path.parent.resolve()),
        str(Path(sys.prefix).resolve()),
        str(Path(sys.base_prefix).resolve()),
        str(Path(sys.executable).resolve().parent),
    }
    for env_name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(env_name):
            readable.add(os.environ[env_name])
    temp_roots = {str(temp_root)} if temp_root is not None else set()
    profile_lines = [
        "(version 1)",
        "(deny default)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow network-outbound)",
        f"(allow process-exec (literal {quoted(sys.executable)}))",
        "(allow file-read-metadata (subpath \"/\"))",
    ]
    profile_lines.extend(
        f"(allow file-read* (subpath {quoted(path)}))"
        for path in sorted(readable)
        if Path(path).exists()
    )
    profile_lines.extend(
        f"(allow file-write* (subpath {quoted(path)}))"
        for path in sorted(temp_roots)
        if Path(path).exists()
    )
    return [str(sandbox_exec), "-p", "\n".join(profile_lines), *python_command]


def _run_sandboxed(scanner_path: Path, payload: dict, timeout: int) -> dict:
    input_data = json.dumps(payload)
    temp_parent = HIVESCANNER_HOME / ".sandbox-tmp"
    try:
        if temp_parent.is_symlink() or (temp_parent.exists() and not temp_parent.is_dir()):
            raise RuntimeError("sandbox temporary path is not a private directory")
        temp_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(temp_parent, 0o700)
    except OSError as exc:
        raise RuntimeError("could not prepare sandbox temporary directory") from exc

    with tempfile.TemporaryDirectory(
        prefix="scanner-", dir=str(temp_parent)
    ) as raw_temp_root:
        temp_root = Path(raw_temp_root).resolve()
        sandbox_env = _sandbox_environment(payload.get("config", {}))
        sandbox_env.update(
            {"TMPDIR": str(temp_root), "TEMP": str(temp_root), "TMP": str(temp_root)}
        )
        kwargs = {
            "text": True,
            "cwd": str(scanner_path.parent),
            "env": sandbox_env,
        }
        if os.name == "posix":
            kwargs["preexec_fn"] = _sandbox_limits
        try:
            # Regular files plus RLIMIT_FSIZE prevent an adapter from making the
            # parent buffer unbounded stdout/stderr through a pipe.
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                process = subprocess.Popen(
                    _sandbox_command(scanner_path, temp_root),
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=os.name == "posix",
                    **kwargs,
                )
                try:
                    process.communicate(input=input_data, timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:  # pragma: no cover - Windows process cleanup
                        process.kill()
                    process.communicate()
                    raise RuntimeError(f"scanner exceeded {timeout}s timeout") from exc
                stdout_file.seek(0)
                stdout_bytes = stdout_file.read(MAX_SANDBOX_OUTPUT_BYTES + 1)
        except RuntimeError:
            raise
    if process.returncode != 0:
        # Community stderr is untrusted and may contain inherited credentials.
        raise RuntimeError(f"scanner exited with status {process.returncode}")
    if len(stdout_bytes) > MAX_SANDBOX_OUTPUT_BYTES:
        raise RuntimeError("scanner output exceeded size limit")
    def reject_constant(value: str) -> None:
        raise ValueError(value)

    def strict_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        output = json.loads(
            stdout_bytes.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=strict_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("scanner returned invalid JSON") from exc
    if not isinstance(output, dict):
        raise RuntimeError("scanner response must be a JSON object")
    return output


def _poll_sandboxed(scanner_path: Path, config: dict, watermark: str) -> tuple[list, str]:
    """Run a 3rd-party scanner in a constrained subprocess."""
    output = _run_sandboxed(
        scanner_path,
        {"command": "poll", "config": config, "watermark": watermark},
        timeout=COMMUNITY_POLL_TIMEOUT_SECONDS,
    )
    pollen = output.get("pollen", output.get("items", []))
    new_watermark = output.get("watermark", watermark)
    if not isinstance(pollen, list):
        raise RuntimeError("scanner 'pollen' must be a JSON array")
    if (
        not isinstance(new_watermark, str)
        or len(new_watermark.encode("utf-8", errors="replace")) > MAX_WATERMARK_BYTES
    ):
        raise RuntimeError("scanner watermark must be a bounded string")
    return pollen, new_watermark


def _check_acted_sandboxed(scanner_path: Path, pollen: dict, config: dict) -> bool:
    """Run a 3rd-party scanner's check_acted in subprocess."""
    try:
        output = _run_sandboxed(
            scanner_path,
            {"command": "check_acted", "item": pollen, "config": config},
            timeout=15,
        )
        return output.get("acted") is True
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
        if py_file.is_symlink() or not py_file.is_file():
            print(
                f"[scanner] ignoring non-regular scanner path {py_file.name}",
                file=sys.stderr,
            )
            continue
        name = py_file.stem
        result[name] = py_file
    return result


def _third_party_supports_check_acted(name: str) -> bool:
    """Only spawn per-item checks when the installed manifest opts in."""
    manifest_path = TEAMMATES_DIR / name / "teammate.json"
    try:
        manifest = load_json(manifest_path, {}, dict)
    except RuntimeError as exc:
        print(f"[scanner] Cannot read manifest for {name}: {exc}", file=sys.stderr)
        return False
    return manifest.get("supports_check_acted") is True


def check_acted_pollen(config: dict, scanners: dict, third_party: dict[str, Path]) -> list[dict]:
    """For each pending pollen, call the scanner's check_acted().
    Returns list of pollen IDs where user acted externally."""
    acted_ids = []
    if not POLLEN_FILE.exists():
        return acted_ids
    hive_data = load_json(POLLEN_FILE, {"pollen": []}, dict)
    if not isinstance(hive_data.get("pollen", []), list):
        raise RuntimeError(f"Cannot read {POLLEN_FILE}: 'pollen' must be a list")

    username = config.get("user", {}).get("username", "")
    candidates = []
    supported_third_party: dict[str, bool] = {}
    for p in hive_data.get("pollen", []):
        if not isinstance(p, dict) or p.get("status") != "pending":
            continue
        source = p.get("source", "")
        if source in scanners and hasattr(scanners[source], "check_acted"):
            candidates.append(p)
        elif source in third_party:
            if source not in supported_third_party:
                supported_third_party[source] = _third_party_supports_check_acted(source)
            if supported_third_party[source]:
                candidates.append(p)

    if not candidates:
        return acted_ids
    cursor_state = load_json(ACTED_CURSOR_FILE, {"cursor": 0}, dict)
    try:
        cursor = max(0, int(cursor_state.get("cursor", 0))) % len(candidates)
    except (TypeError, ValueError):
        cursor = 0
    selected = [
        candidates[(cursor + offset) % len(candidates)]
        for offset in range(min(MAX_ACTED_CHECKS_PER_RUN, len(candidates)))
    ]
    atomic_write_json(
        ACTED_CURSOR_FILE,
        {"cursor": (cursor + len(selected)) % len(candidates)},
    )

    for p in selected:
        source = p.get("source", "")
        scanner_config = dict(config.get("scanners", {}).get(source, {}))
        scanner_config["_username"] = username

        if source in scanners:
            scanner = scanners[source]
            try:
                if scanner.check_acted(p, scanner_config):
                    if p.get("id"):
                        acted_ids.append({"source": source, "id": p["id"]})
            except Exception as e:
                print(f"[scanner] check_acted error for {source}: {e}", file=sys.stderr)
        elif source in third_party:
            if _check_acted_sandboxed(third_party[source], p, scanner_config):
                if p.get("id"):
                    acted_ids.append({"source": source, "id": p["id"]})

    return acted_ids


def poll_all(
    config: dict,
    scanners: dict,
    third_party: dict[str, Path],
    watermarks: dict,
    known_keys: set[str] | None = None,
    max_items: int | None = MAX_POLLEN_PER_CYCLE,
) -> tuple[list, list]:
    """Poll all enabled scanners. Returns (pollen, acted_ids)."""
    tagged_pollen: list[tuple[str, dict]] = []
    new_watermarks: dict[str, str] = {}
    contributed_counts: dict[str, int] = {}
    seen_keys = set(known_keys or set())

    for scanner_name, scanner_config in config.get("scanners", {}).items():
        if not scanner_config.get("enabled"):
            continue

        effective_config = dict(scanner_config)
        effective_config.setdefault("_username", config.get("user", {}).get("username", ""))
        watermark = watermarks.get(scanner_name, "1970-01-01T00:00:00Z")

        pollen = None
        new_wm = None
        if scanner_name in scanners:
            try:
                pollen, new_wm = scanners[scanner_name].poll(effective_config, watermark)
            except Exception as e:
                print(f"[scanner] Error polling {scanner_name}: {e}", file=sys.stderr)
                continue
        elif scanner_name in third_party:
            try:
                pollen, new_wm = _poll_sandboxed(third_party[scanner_name], effective_config, watermark)
            except Exception as e:
                print(f"[scanner] Error polling sandboxed {scanner_name}: {e}", file=sys.stderr)
                continue
        else:
            continue

        if not isinstance(pollen, list):
            print(f"[scanner] Invalid pollen list from {scanner_name}; preserving watermark", file=sys.stderr)
            continue
        if len(pollen) > MAX_ITEMS_PER_SCANNER:
            print(
                f"[scanner] {scanner_name} returned more than {MAX_ITEMS_PER_SCANNER} items; "
                "preserving watermark",
                file=sys.stderr,
            )
            continue
        if (
            not isinstance(new_wm, str)
            or len(new_wm.encode("utf-8", errors="replace")) > MAX_WATERMARK_BYTES
        ):
            print(f"[scanner] Invalid watermark from {scanner_name}; preserving watermark", file=sys.stderr)
            continue

        normalized_items = []
        invalid_item = False
        for raw_item in pollen:
            item = normalize_pollen(raw_item, expected_source=scanner_name)
            if item is None:
                invalid_item = True
                print(f"[scanner] Invalid pollen item from {scanner_name}; preserving watermark", file=sys.stderr)
                continue
            key = pollen_key(scanner_name, item["id"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            normalized_items.append(item)

        if not invalid_item:
            new_watermarks[scanner_name] = new_wm
        contributed_counts[scanner_name] = len(normalized_items)
        for item in normalized_items:
            tagged_pollen.append((scanner_name, item))

    if max_items is not None and max_items >= 0 and len(tagged_pollen) > max_items:
        # Round-robin interleave by scanner so no single scanner hogs the cap.
        # Preserves per-scanner order; scanners with fewer items get served fully.
        by_scanner: dict[str, deque[dict]] = {}
        order: list[str] = []
        for scanner_name, item in tagged_pollen:
            if scanner_name not in by_scanner:
                order.append(scanner_name)
                by_scanner[scanner_name] = deque()
            by_scanner[scanner_name].append(item)

        interleaved: list[tuple[str, dict]] = []
        while len(interleaved) < max_items:
            drained = True
            for name in order:
                bucket = by_scanner[name]
                if bucket:
                    interleaved.append((name, bucket.popleft()))
                    drained = False
                    if len(interleaved) >= max_items:
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


def _wait_for_next_poll(seconds: float) -> None:
    """Wait responsively so SIGTERM/SIGINT/SIGHUP shut down within one second."""
    deadline = time.monotonic() + seconds
    while not _shutdown_requested:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def main():
    acquire_lock()
    try:
        config = load_config()
        preflight(config)  # Auto-install missing CLI deps for enabled scanners
        watermarks = load_watermarks()
        scanners = load_scanners()
        third_party = get_third_party_scanners()

        pending_delivery = reconcile_pending_batch(watermarks)
        if pending_delivery is not None:
            pending_pollen, pending_acted_ids = pending_delivery
            output_pollen(pending_pollen, pending_acted_ids)
            return

        while True:
            if _shutdown_requested:
                save_watermarks(watermarks)
                break

            # Manager commands and manual config edits take effect without waiting
            # for some unrelated pollen to make this one-shot process restart.
            config = load_config()
            third_party = get_third_party_scanners()
            poll_interval = config.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL)
            known_keys = load_pollen_keys()
            available_slots = max(0, MAX_QUEUE_ITEMS - pollen_queue_count())
            if available_slots == 0:
                output_error(
                    f"Pollen queue is full ({MAX_QUEUE_ITEMS} items); dismiss/prune items before polling"
                )
                break

            new_pollen, acted_ids = poll_all(
                config,
                scanners,
                third_party,
                watermarks,
                known_keys=known_keys,
                max_items=min(available_slots, MAX_NEW_ITEMS_PER_RUN),
            )

            if new_pollen or acted_ids:
                delivery, delivery_acted_ids = save_pending_batch(
                    new_pollen, acted_ids, watermarks
                )
                output_pollen(delivery, delivery_acted_ids)
                break

            # Quiet cycles are safe to commit immediately because there is no
            # external handoff that can be lost after the watermark advances.
            save_watermarks(watermarks)
            _wait_for_next_poll(poll_interval)

    except Exception as e:
        output_error(str(e))
        sys.exit(1)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
