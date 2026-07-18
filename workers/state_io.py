"""Safe JSON state-file helpers used by HiveScanner workers."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX only
    msvcrt = None


class StateFileError(RuntimeError):
    """Raised when persisted state cannot be read without risking data loss."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r} is not allowed")
        result[key] = value
    return result


@contextmanager
def advisory_lock(path: Path):
    """Hold a cross-process advisory lock for a read/modify/write transaction."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(
        path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise StateFileError(f"Cannot lock {path}: lock path is not a regular file")
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    windows_locked = False
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows only
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            windows_locked = True
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif windows_locked and msvcrt is not None:  # pragma: no cover - Windows only
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def load_json(
    path: Path,
    default: Any,
    expected_type: type | tuple[type, ...],
    *,
    max_bytes: int = 128 * 1024 * 1024,
) -> Any:
    """Load JSON state, rejecting corrupt or structurally invalid files.

    Missing files use ``default``. Existing files never silently fall back: doing
    so would let the next write overwrite the only copy of the user's state.
    """
    try:
        before = path.lstat()
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise StateFileError(f"Cannot inspect {path}: {exc}; file was left untouched") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise StateFileError(
            f"Cannot read {path}: state path must be a regular file, not a symlink or device; "
            "file was left untouched"
        )
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
        except Exception:
            os.close(fd)
            raise
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            os.close(fd)
            raise StateFileError(
                f"Cannot read {path}: state path changed while opening; file was left untouched"
            )
        size = opened.st_size
        if size > max_bytes:
            os.close(fd)
            raise StateFileError(
                f"Cannot read {path}: file is {size} bytes (limit {max_bytes}); "
                "file was left untouched"
            )
        with os.fdopen(fd, "rb") as handle:
            raw_bytes = handle.read(max_bytes + 1)
        if len(raw_bytes) > max_bytes:
            raise StateFileError(
                f"Cannot read {path}: file exceeded {max_bytes} bytes while reading; "
                "file was left untouched"
            )
        value = json.loads(
            raw_bytes,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_strict_object,
        )
    except StateFileError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError) as exc:
        raise StateFileError(f"Cannot read {path}: {exc}; file was left untouched") from exc
    if not isinstance(value, expected_type):
        expected = (
            ", ".join(t.__name__ for t in expected_type)
            if isinstance(expected_type, tuple)
            else expected_type.__name__
        )
        raise StateFileError(
            f"Cannot read {path}: expected top-level {expected}, got {type(value).__name__}; "
            "file was left untouched"
        )
    return value


def atomic_write_json(path: Path, value: Any, *, indent: int = 2) -> None:
    """Atomically replace a JSON file with a private, fsynced temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp_path: Path | None = None
    try:
        fd, raw_tmp_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(raw_tmp_path)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, indent=indent, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.replace(str(tmp_path), str(path))
        tmp_path = None
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # Persist the directory entry as well as the file contents. Without
        # this fsync, a power loss can still forget an otherwise atomic rename.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some platforms/filesystems do not support directory fsync.
            pass
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
