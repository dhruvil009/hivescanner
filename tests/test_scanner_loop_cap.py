"""Tests for scanner_loop.poll_all watermark advancement under batch cap."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
import scanner_loop


class FakeScanner:
    def __init__(self, pollen, wm, name):
        self._pollen = pollen
        self._wm = wm
        self.name = name

    def poll(self, config, watermark):
        return list(self._pollen), self._wm


class RaisingScanner:
    def __init__(self, name):
        self.name = name

    def poll(self, config, watermark):
        raise RuntimeError("boom")


def _item(pid, source):
    return {
        "id": pid,
        "source": source,
        "type": "x",
        "title": "t",
        "discovered_at": "2026-04-13T00:00:00Z",
    }


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    monkeypatch.setattr(scanner_loop, "check_acted_pollen", lambda *a, **kw: [])


def _config(*names):
    return {"scanners": {n: {"enabled": True} for n in names}}


class TestPollAllCap:
    def test_no_overflow_both_advance(self):
        scanners = {
            "a": FakeScanner([_item(f"a{i}", "a") for i in range(5)], "2026-04-13T10:00:00Z", "a"),
            "b": FakeScanner([_item(f"b{i}", "b") for i in range(5)], "2026-04-13T11:00:00Z", "b"),
        }
        watermarks = {}
        pollen, acted = scanner_loop.poll_all(_config("a", "b"), scanners, {}, watermarks)

        assert len(pollen) == 10
        assert acted == []
        assert watermarks["a"] == "2026-04-13T10:00:00Z"
        assert watermarks["b"] == "2026-04-13T11:00:00Z"

    def test_overflow_one_scanner_overflows(self):
        scanners = {
            "a": FakeScanner([_item(f"a{i}", "a") for i in range(5)], "2026-04-13T10:00:00Z", "a"),
            "b": FakeScanner([_item(f"b{i}", "b") for i in range(20)], "2026-04-13T11:00:00Z", "b"),
        }
        watermarks = {}
        pollen, _ = scanner_loop.poll_all(_config("a", "b"), scanners, {}, watermarks)

        assert len(pollen) == scanner_loop.MAX_POLLEN_PER_CYCLE == 20
        assert watermarks.get("a") == "2026-04-13T10:00:00Z"
        assert "b" not in watermarks

    def test_overflow_both_at_boundary(self):
        scanners = {
            "a": FakeScanner([_item(f"a{i}", "a") for i in range(20)], "2026-04-13T10:00:00Z", "a"),
            "b": FakeScanner([_item(f"b{i}", "b") for i in range(5)], "2026-04-13T11:00:00Z", "b"),
        }
        watermarks = {}
        pollen, _ = scanner_loop.poll_all(_config("a", "b"), scanners, {}, watermarks)

        assert len(pollen) == 20
        assert watermarks.get("a") == "2026-04-13T10:00:00Z"
        assert "b" not in watermarks

    def test_exception_during_poll(self):
        scanners = {
            "a": RaisingScanner("a"),
            "b": FakeScanner([_item(f"b{i}", "b") for i in range(3)], "2026-04-13T11:00:00Z", "b"),
        }
        watermarks = {}
        pollen, _ = scanner_loop.poll_all(_config("a", "b"), scanners, {}, watermarks)

        assert len(pollen) == 3
        assert "a" not in watermarks
        assert watermarks["b"] == "2026-04-13T11:00:00Z"

    def test_empty_poll_advances_watermark(self):
        scanners = {
            "a": FakeScanner([], "2026-04-13T10:00:00Z", "a"),
        }
        watermarks = {}
        pollen, _ = scanner_loop.poll_all(_config("a"), scanners, {}, watermarks)

        assert pollen == []
        assert watermarks["a"] == "2026-04-13T10:00:00Z"
