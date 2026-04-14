"""Windows-path tests for scanner_loop.is_pid_running.

The real Windows code is unreachable on macOS/Linux CI, so we monkeypatch
sys.platform to "win32" and stub ctypes.windll.kernel32 with canned values
to exercise each of the three branches: handle, access-denied, nope.
"""

from __future__ import annotations

import ctypes
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "workers"))
import scanner_loop  # noqa: E402


def _make_kernel32(open_handle, exit_code_value, get_exit_ok=1, last_error=0):
    kernel32 = MagicMock()
    kernel32.OpenProcess.return_value = open_handle

    def _get_exit_code(handle, byref_obj):
        # byref_obj is a ctypes byref pointing at the c_ulong; mutate via _obj.
        byref_obj._obj.value = exit_code_value
        return get_exit_ok

    kernel32.GetExitCodeProcess.side_effect = _get_exit_code
    kernel32.CloseHandle.return_value = 1
    kernel32.GetLastError.return_value = last_error
    return kernel32


def _install_windll(monkeypatch, kernel32):
    windll = MagicMock()
    windll.kernel32 = kernel32
    monkeypatch.setattr("ctypes.windll", windll, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")


def test_windows_running_process_returns_true(monkeypatch):
    kernel32 = _make_kernel32(open_handle=0xABCD, exit_code_value=259)
    _install_windll(monkeypatch, kernel32)
    assert scanner_loop.is_pid_running(1234) is True
    kernel32.CloseHandle.assert_called_once()


def test_windows_exited_process_returns_false(monkeypatch):
    kernel32 = _make_kernel32(open_handle=0xABCD, exit_code_value=0)
    _install_windll(monkeypatch, kernel32)
    assert scanner_loop.is_pid_running(1234) is False
    kernel32.CloseHandle.assert_called_once()


def test_windows_access_denied_returns_true(monkeypatch):
    kernel32 = _make_kernel32(open_handle=0, exit_code_value=0, last_error=5)
    _install_windll(monkeypatch, kernel32)
    assert scanner_loop.is_pid_running(1234) is True
    kernel32.GetExitCodeProcess.assert_not_called()


def test_windows_invalid_parameter_returns_false(monkeypatch):
    kernel32 = _make_kernel32(open_handle=0, exit_code_value=0, last_error=87)
    _install_windll(monkeypatch, kernel32)
    assert scanner_loop.is_pid_running(1234) is False
    kernel32.GetExitCodeProcess.assert_not_called()


def test_windows_get_exit_code_failure_assumes_running(monkeypatch):
    kernel32 = _make_kernel32(open_handle=0xABCD, exit_code_value=0, get_exit_ok=0)
    _install_windll(monkeypatch, kernel32)
    assert scanner_loop.is_pid_running(1234) is True


@pytest.mark.parametrize("pid", [0, -1, -1234])
def test_nonpositive_pid_returns_false_without_touching_ctypes(monkeypatch, pid):
    sentinel = MagicMock()
    sentinel.kernel32.OpenProcess.side_effect = AssertionError("ctypes should not be touched")
    monkeypatch.setattr("ctypes.windll", sentinel, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert scanner_loop.is_pid_running(pid) is False
    sentinel.kernel32.OpenProcess.assert_not_called()
