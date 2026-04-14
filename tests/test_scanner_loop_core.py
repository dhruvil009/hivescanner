"""Tests for scanner_loop.py: lock acquisition and multi-scanner failure isolation.

Complements tests/test_scanner_loop_cap.py (batch cap + watermark advancement).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "workers"))
import scanner_loop  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def no_acted(monkeypatch):
    """check_acted_pollen reads POLLEN_FILE — stub for hermetic tests."""
    monkeypatch.setattr(scanner_loop, "check_acted_pollen", lambda *a, **kw: [])


@pytest.fixture
def hermetic_home(monkeypatch, tmp_path):
    """Redirect HIVESCANNER_HOME (+ derived paths) to a tmp dir for in-process tests."""
    home = tmp_path / ".hivescanner"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(scanner_loop, "HIVESCANNER_HOME", home)
    monkeypatch.setattr(scanner_loop, "LOCK_FILE", home / ".lock")
    monkeypatch.setattr(scanner_loop, "WATERMARKS_FILE", home / "watermarks.json")
    monkeypatch.setattr(scanner_loop, "POLLEN_FILE", home / "pollen.json")
    monkeypatch.setattr(scanner_loop, "CONFIG_FILE", home / "config.json")
    # Reset any leftover module-level fd from prior tests.
    monkeypatch.setattr(scanner_loop, "_META_LOCK_FD", None, raising=False)
    return home


class FakeScanner:
    def __init__(self, name, pollen, wm):
        self.name = name
        self._pollen = pollen
        self._wm = wm

    def poll(self, config, watermark):
        return list(self._pollen), self._wm


class RaisingScanner:
    def __init__(self, name, exc=None):
        self.name = name
        self._exc = exc or RuntimeError("boom")

    def poll(self, config, watermark):
        raise self._exc


def _item(pid, source):
    return {
        "id": pid,
        "source": source,
        "type": "x",
        "title": "t",
        "discovered_at": "2026-04-13T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Lock tests — POSIX flock path. Skipped on Windows (different code path).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock path only")
class TestLockPosix:
    def test_acquire_release_cycle(self, hermetic_home):
        """acquire writes PID + flock file; release removes lock file; reacquire works."""
        scanner_loop.acquire_lock()
        try:
            lock_file = hermetic_home / ".lock"
            flock_file = hermetic_home / ".lock.flock"
            assert lock_file.exists(), "LOCK_FILE should exist after acquire"
            assert flock_file.exists(), "flock sidecar should exist after acquire"
            assert lock_file.read_text().strip() == str(os.getpid())
            assert scanner_loop._META_LOCK_FD is not None
        finally:
            scanner_loop.release_lock()

        assert not (hermetic_home / ".lock").exists(), "LOCK_FILE removed on release"
        assert scanner_loop._META_LOCK_FD is None

        # Reacquire after release should work cleanly.
        scanner_loop.acquire_lock()
        try:
            assert (hermetic_home / ".lock").exists()
        finally:
            scanner_loop.release_lock()

    def test_acquire_mutual_exclusion(self, tmp_path):
        """Second process trying to acquire an already-held lock exits(1) with error JSON."""
        env = {**os.environ, "HOME": str(tmp_path)}

        holder_code = (
            "import sys; sys.path.insert(0, 'workers'); "
            "import scanner_loop; scanner_loop.acquire_lock(); "
            "import time; time.sleep(5); scanner_loop.release_lock()"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code],
            env=env,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Wait for holder to actually acquire.
            lock_path = tmp_path / ".hivescanner" / ".lock"
            deadline = time.time() + 3.0
            while time.time() < deadline and not lock_path.exists():
                time.sleep(0.05)
            assert lock_path.exists(), "holder never created the lock file"

            challenger_code = (
                "import sys; sys.path.insert(0, 'workers'); "
                "import scanner_loop; scanner_loop.acquire_lock()"
            )
            challenger = subprocess.run(
                [sys.executable, "-c", challenger_code],
                env=env,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=8,
            )
            assert challenger.returncode == 1, (
                f"expected exit 1, got {challenger.returncode}. "
                f"stdout={challenger.stdout!r} stderr={challenger.stderr!r}"
            )
            assert "Another scanner loop running" in challenger.stdout
            # Parse the JSON payload to confirm output_error format.
            payload = json.loads(challenger.stdout.strip().splitlines()[-1])
            assert payload["type"] == "error"
            assert "Another scanner loop running" in payload["message"]
        finally:
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=2)

    def test_acquire_reacquire_after_death(self, tmp_path):
        """flock auto-releases on process death; next acquire should succeed."""
        env = {**os.environ, "HOME": str(tmp_path)}

        # Subprocess acquires but does not call release_lock — simulates a crash.
        first = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, 'workers'); "
                "import scanner_loop; scanner_loop.acquire_lock()",
            ],
            env=env,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert first.returncode == 0, (
            f"first acquire failed: stdout={first.stdout!r} stderr={first.stderr!r}"
        )
        # At this point, kernel has released the flock. LOCK_FILE may still exist
        # (stale PID file), but a new acquirer should succeed via flock.
        second = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, 'workers'); "
                "import scanner_loop; scanner_loop.acquire_lock(); "
                "scanner_loop.release_lock()",
            ],
            env=env,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert second.returncode == 0, (
            f"second acquire after death failed: "
            f"stdout={second.stdout!r} stderr={second.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Scanner failure isolation tests (in-process).
# ---------------------------------------------------------------------------


def _enabled_config(*names):
    return {"scanners": {n: {"enabled": True} for n in names}}


class TestScannerFailureIsolation:
    def test_scanner_error_isolated_from_others(self, no_acted, capsys):
        """A raises, B+C succeed: pollen from B+C returned, only A's watermark unchanged."""
        scanners = {
            "A": RaisingScanner("A", RuntimeError("boom")),
            "B": FakeScanner("B", [_item(f"b{i}", "B") for i in range(2)], "2026-04-13T10:00:00Z"),
            "C": FakeScanner("C", [_item(f"c{i}", "C") for i in range(3)], "2026-04-13T11:00:00Z"),
        }
        watermarks = {}
        pollen, acted = scanner_loop.poll_all(
            _enabled_config("A", "B", "C"), scanners, {}, watermarks
        )

        assert len(pollen) == 5, f"expected 2+3=5 pollen, got {len(pollen)}"
        assert acted == []
        assert "A" not in watermarks, "A raised, its watermark must not advance"
        assert watermarks["B"] == "2026-04-13T10:00:00Z"
        assert watermarks["C"] == "2026-04-13T11:00:00Z"

        captured = capsys.readouterr()
        assert "Error polling A" in captured.err
        assert "boom" in captured.err

    def test_sandboxed_scanner_error_isolated(self, no_acted, monkeypatch, capsys):
        """Third-party sandboxed scanner failure does not break other sandboxed scanners."""
        y_wm = "2026-04-13T00:00:00Z"
        y_item = _item("y0", "Y")

        def fake_poll_sandboxed(path, config, watermark):
            # Path is the value we put in third_party[name]; dispatch on its name.
            name = Path(path).name
            if name == "X":
                raise RuntimeError("sandbox exploded")
            if name == "Y":
                return [y_item], y_wm
            raise AssertionError(f"unexpected scanner path: {path}")

        monkeypatch.setattr(scanner_loop, "_poll_sandboxed", fake_poll_sandboxed)

        third_party = {"X": Path("/fake/X"), "Y": Path("/fake/Y")}
        watermarks = {}
        pollen, acted = scanner_loop.poll_all(
            _enabled_config("X", "Y"), {}, third_party, watermarks
        )

        assert len(pollen) == 1
        assert pollen[0]["id"] == "y0"
        assert acted == []
        assert "X" not in watermarks, "sandboxed X failed → watermark not advanced"
        assert watermarks["Y"] == y_wm

        captured = capsys.readouterr()
        assert "Error polling sandboxed X" in captured.err
        assert "sandbox exploded" in captured.err

    def test_missing_scanner_in_config_is_skipped(self, no_acted):
        """Config refers to a scanner that exists in neither dict: skip silently."""
        watermarks = {}
        pollen, acted = scanner_loop.poll_all(
            {"scanners": {"ghost": {"enabled": True}}},
            {},  # no built-in
            {},  # no third-party
            watermarks,
        )

        assert pollen == []
        assert acted == []
        assert watermarks == {}
