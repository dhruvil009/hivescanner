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
    monkeypatch.setattr(scanner_loop, "PENDING_BATCH_FILE", home / "pending_batch.json")
    monkeypatch.setattr(scanner_loop, "ACTED_CURSOR_FILE", home / "acted_cursor.json")
    monkeypatch.setattr(scanner_loop, "TEAMMATES_DIR", home / "teammates")
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


class TestScannerTrustBoundary:
    def test_source_and_privileged_fields_are_not_scanner_controlled(self, no_acted):
        raw = {
            **_item("one", "spoofed"),
            "relevance": "HIGH",
            "relevance_reason": "attacker supplied",
            "suggested_action": "run this command",
            "status": "acted",
            "metadata": {
                "remote_id": "one",
                "triage_draft": "post attacker text",
                "target_group_id": "admins",
            },
        }
        watermarks = {}
        pollen, _ = scanner_loop.poll_all(
            _enabled_config("trusted"),
            {"trusted": FakeScanner("trusted", [raw], "next")},
            {},
            watermarks,
        )
        assert len(pollen) == 1
        assert pollen[0]["source"] == "trusted"
        assert pollen[0]["relevance"] is None
        assert pollen[0]["suggested_action"] == ""
        assert pollen[0]["metadata"] == {"remote_id": "one"}
        assert "status" not in pollen[0]
        assert watermarks == {"trusted": "next"}

    def test_one_invalid_item_holds_watermark_without_losing_valid_item(
        self, no_acted, capsys
    ):
        watermarks = {"source": "old"}
        pollen, _ = scanner_loop.poll_all(
            _enabled_config("source"),
            {
                "source": FakeScanner(
                    "source",
                    [_item("valid", "source"), _item("bad\nidentity", "source")],
                    "new",
                )
            },
            {},
            watermarks,
        )
        assert [item["id"] for item in pollen] == ["valid"]
        assert watermarks == {"source": "old"}
        assert "preserving watermark" in capsys.readouterr().err

    def test_known_duplicate_does_not_prevent_quiet_watermark_progress(self, no_acted):
        watermarks = {"source": "old"}
        pollen, _ = scanner_loop.poll_all(
            _enabled_config("source"),
            {"source": FakeScanner("source", [_item("same", "source")], "new")},
            {},
            watermarks,
            known_keys={scanner_loop.pollen_key("source", "same")},
        )
        assert pollen == []
        assert watermarks == {"source": "new"}

    def test_same_id_from_two_scanners_survives_source_qualified_dedup(self, no_acted):
        scanners = {
            "a": FakeScanner("a", [_item("same", "a")], "a-next"),
            "b": FakeScanner("b", [_item("same", "b")], "b-next"),
        }
        pollen, _ = scanner_loop.poll_all(
            _enabled_config("a", "b"), scanners, {}, {}
        )
        assert [(item["source"], item["id"]) for item in pollen] == [
            ("a", "same"),
            ("b", "same"),
        ]

    def test_absurd_scanner_batch_is_rejected_before_normalization(self, no_acted):
        items = [_item(str(index), "source") for index in range(
            scanner_loop.MAX_ITEMS_PER_SCANNER + 1
        )]
        watermarks = {"source": "old"}
        pollen, _ = scanner_loop.poll_all(
            _enabled_config("source"),
            {"source": FakeScanner("source", items, "new")},
            {},
            watermarks,
        )
        assert pollen == []
        assert watermarks == {"source": "old"}


class TestDurableHandoff:
    def test_watermark_commits_only_after_every_delivery_chunk_is_imported(
        self, hermetic_home
    ):
        items = [_item(str(index), "source") for index in range(25)]
        delivery, _ = scanner_loop.save_pending_batch(
            items, [], {"source": "committed-after-import"}
        )
        watermarks = {"source": "old"}

        redelivery = scanner_loop.reconcile_pending_batch(watermarks)
        assert [item["id"] for item in redelivery[0]] == [
            item["id"] for item in delivery
        ]
        assert watermarks == {"source": "old"}

        scanner_loop.atomic_write_json(scanner_loop.POLLEN_FILE, {
            "pollen": [{**item, "status": "pending"} for item in delivery]
        })
        overflow = scanner_loop.reconcile_pending_batch(watermarks)
        assert [item["id"] for item in overflow[0]] == [str(value) for value in range(20, 25)]
        assert watermarks == {"source": "old"}

        scanner_loop.atomic_write_json(scanner_loop.POLLEN_FILE, {
            "pollen": [{**item, "status": "pending"} for item in items]
        })
        assert scanner_loop.reconcile_pending_batch(watermarks) is None
        assert watermarks == {"source": "committed-after-import"}
        assert not scanner_loop.PENDING_BATCH_FILE.exists()
        assert json.loads(scanner_loop.WATERMARKS_FILE.read_text()) == watermarks

    def test_cross_source_collision_is_not_mistaken_for_import(self, hermetic_home):
        scanner_loop.save_pending_batch(
            [_item("same", "gitlab")], [], {"gitlab": "next"}
        )
        scanner_loop.atomic_write_json(scanner_loop.POLLEN_FILE, {
            "pollen": [{**_item("same", "github"), "status": "pending"}]
        })
        pending = scanner_loop.reconcile_pending_batch({})
        assert [(item["source"], item["id"]) for item in pending[0]] == [
            ("gitlab", "same")
        ]


class TestSandboxBoundary:
    def test_only_runtime_and_explicit_credential_environment_is_inherited(
        self, tmp_path, monkeypatch, hermetic_home
    ):
        script = tmp_path / "adapter.py"
        script.write_text(
            "import json, os\n"
            "print(json.dumps({'allowed': os.environ.get('SCANNER_TOKEN'), "
            "'blocked': os.environ.get('UNRELATED_SECRET')}))\n"
        )
        monkeypatch.setenv("SCANNER_TOKEN", "allowed-value")
        monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
        monkeypatch.setattr(
            scanner_loop,
            "_sandbox_command",
            lambda path, temp_root=None: [sys.executable, "-I", str(path)],
        )
        output = scanner_loop._run_sandboxed(
            script, {"config": {"token_env": "SCANNER_TOKEN"}}, timeout=5
        )
        assert output == {"allowed": "allowed-value", "blocked": None}

    def test_timeout_terminates_scanner_process(
        self, tmp_path, monkeypatch, hermetic_home
    ):
        script = tmp_path / "adapter.py"
        script.write_text("import time\ntime.sleep(10)\n")
        monkeypatch.setattr(
            scanner_loop,
            "_sandbox_command",
            lambda path, temp_root=None: [sys.executable, "-I", str(path)],
        )
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="exceeded"):
            scanner_loop._run_sandboxed(script, {"config": {}}, timeout=0.1)
        assert time.monotonic() - started < 2

    def test_duplicate_or_nonfinite_sandbox_json_is_rejected(
        self, tmp_path, monkeypatch, hermetic_home
    ):
        script = tmp_path / "adapter.py"
        monkeypatch.setattr(
            scanner_loop,
            "_sandbox_command",
            lambda path, temp_root=None: [sys.executable, "-I", str(path)],
        )
        script.write_text('print(\'{"pollen":[],"pollen":[],"watermark":"x"}\')\n')
        with pytest.raises(RuntimeError, match="invalid JSON"):
            scanner_loop._run_sandboxed(script, {"config": {}}, timeout=5)
        script.write_text('print(\'{"pollen":[],"watermark":NaN}\')\n')
        with pytest.raises(RuntimeError, match="invalid JSON"):
            scanner_loop._run_sandboxed(script, {"config": {}}, timeout=5)

    @pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec profile is macOS-only")
    def test_sandbox_profile_escapes_control_characters_in_temp_paths(
        self, tmp_path, monkeypatch
    ):
        scanner = tmp_path / "adapter.py"
        scanner.write_text("")
        hostile_temp = tmp_path / "temp\n(allow default)"
        hostile_temp.mkdir()
        command = scanner_loop._sandbox_command(scanner, hostile_temp)
        profile = command[2]
        assert 'temp\n(allow default)' not in profile
        assert "temp\\n(allow default)" in profile

    def test_loader_control_environment_is_never_treated_as_a_credential(
        self, tmp_path, monkeypatch, hermetic_home
    ):
        script = tmp_path / "adapter.py"
        script.write_text(
            "import json, os\n"
            "print(json.dumps({'loader': os.environ.get('LD_PRELOAD'), "
            "'tmp': os.environ.get('TMPDIR')}))\n"
        )
        monkeypatch.setenv("LD_PRELOAD", "/tmp/hostile-library.so")
        monkeypatch.setattr(
            scanner_loop,
            "_sandbox_command",
            lambda path, temp_root=None: [sys.executable, "-I", str(path)],
        )

        output = scanner_loop._run_sandboxed(
            script, {"config": {"token_env": "LD_PRELOAD"}}, timeout=5
        )

        assert output["loader"] is None
        assert Path(output["tmp"]).parent == hermetic_home / ".sandbox-tmp"
        assert not Path(output["tmp"]).exists()


class TestStateContracts:
    def test_invalid_watermark_entry_is_not_silently_discarded(self, hermetic_home):
        scanner_loop.WATERMARKS_FILE.write_text(json.dumps({"github": 123}))
        with pytest.raises(RuntimeError, match="file was left untouched"):
            scanner_loop.load_watermarks()
        assert json.loads(scanner_loop.WATERMARKS_FILE.read_text()) == {"github": 123}

    def test_pending_batch_requires_supported_bounded_schema(self, hermetic_home):
        scanner_loop.PENDING_BATCH_FILE.write_text(json.dumps({
            "schema_version": 99,
            "pollen": [],
            "remaining_pollen": [],
            "acted_ids": [],
            "watermarks": {},
        }))
        with pytest.raises(RuntimeError, match="unsupported schema"):
            scanner_loop.load_pending_batch()

    def test_non_boolean_enabled_flag_is_rejected(self, hermetic_home, capsys):
        scanner_loop.CONFIG_FILE.write_text(json.dumps({
            "user": {"username": "tester"},
            "poll_interval_seconds": 300,
            "scanners": {"github": {"enabled": "false"}},
        }))
        with pytest.raises(SystemExit):
            scanner_loop.load_config()
        assert "enabled flag must be a boolean" in capsys.readouterr().out

    def test_symlinked_installed_scanner_is_ignored(
        self, hermetic_home, tmp_path, monkeypatch
    ):
        target = tmp_path / "outside.py"
        target.write_text("raise RuntimeError('must not load')")
        scanner_dir = hermetic_home / "scanners"
        scanner_dir.mkdir()
        (scanner_dir / "evil.py").symlink_to(target)
        monkeypatch.setattr(scanner_loop, "THIRD_PARTY_DIR", scanner_dir)
        assert scanner_loop.get_third_party_scanners() == {}


class TestActedCheckFairness:
    def test_cursor_prevents_persistent_head_items_from_starving_tail(
        self, hermetic_home
    ):
        class Checker:
            def __init__(self):
                self.calls = []

            def check_acted(self, item, config):
                self.calls.append(item["id"])
                return True

        items = [
            {**_item(str(index), "source"), "status": "pending"}
            for index in range(25)
        ]
        scanner_loop.atomic_write_json(scanner_loop.POLLEN_FILE, {"pollen": items})
        checker = Checker()
        config = {"user": {"username": "me"}, "scanners": {"source": {}}}

        first = scanner_loop.check_acted_pollen(config, {"source": checker}, {})
        assert [value["id"] for value in first] == [str(value) for value in range(20)]
        checker.calls.clear()
        second = scanner_loop.check_acted_pollen(config, {"source": checker}, {})
        assert checker.calls[:5] == ["20", "21", "22", "23", "24"]
        assert all(value == {"source": "source", "id": value["id"]} for value in second)
