"""Safety checks for opt-in CLI dependency installation."""

import os
import subprocess
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
import dep_installer  # noqa: E402


def test_run_install_accepts_success_without_reading_unbounded_output(monkeypatch):
    def fake_run(cmd, *, stdout, stderr, timeout):
        stdout.write(b"x" * 10_000)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(dep_installer.subprocess, "run", fake_run)
    assert dep_installer._run_install(["package-manager", "install"]) is True


def test_run_install_bounds_and_safely_decodes_failure(monkeypatch, capsys):
    def fake_run(cmd, *, stdout, stderr, timeout):
        stderr.write(b"\xff" + b"x" * 10_000)
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(dep_installer.subprocess, "run", fake_run)
    assert dep_installer._run_install(["package-manager", "install"]) is False
    diagnostic = capsys.readouterr().err
    assert "failed (package-manager)" in diagnostic
    assert "\ufffd" in diagnostic
    assert len(diagnostic) < 300


def test_run_install_fails_closed_on_os_error(monkeypatch):
    def fake_run(cmd, *, stdout, stderr, timeout):
        raise OSError("cannot execute")

    monkeypatch.setattr(dep_installer.subprocess, "run", fake_run)
    assert dep_installer._run_install(["package-manager", "install"]) is False
