"""Functional tests for weather state, timezone, and alert transitions."""

import hashlib
import importlib.util
import os
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request
from unittest.mock import patch

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
_PATH = os.path.join(os.path.dirname(__file__), "..", "workers", "sources", "weather.py")
_spec = importlib.util.spec_from_file_location("weather", _PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["weather"] = _mod
_spec.loader.exec_module(_mod)
WeatherScanner = _mod.WeatherScanner


WEATHER = {
    "current_condition": [{
        "temp_C": "22",
        "humidity": "55",
        "weatherDesc": [{"value": "Partly Cloudy"}],
    }],
    "weather": [{"maxtempC": "28", "mintempC": "15"}],
}


def _scope(location="London", timezone_name="UTC"):
    return hashlib.sha256(
        f"{location.casefold()}\0{timezone_name}".encode()
    ).hexdigest()[:16]


def _scanner(*, ready=False, committed=None):
    with (
        patch.object(_mod, "load_snapshot", return_value={}),
        patch.object(_mod, "snapshot_exists", return_value=ready),
    ):
        scanner = WeatherScanner()
    if ready:
        current = committed or {
            "scope": _scope(),
            "last_temp": 22,
            "last_desc": "Partly Cloudy",
            "morning_dates": {},
        }
        scanner._snapshot = {
            "schema_version": 2,
            "committed": current,
            "candidate": {
                **current,
                "morning_dates": dict(current.get("morning_dates", {})),
            },
            "candidate_watermark": "1970-01-01T00:00:00Z",
            "bootstrap_pending": False,
        }
    return scanner


def _config(scanner, **changes):
    return {
        **scanner.configure(),
        "location": "London",
        "timezone": "UTC",
        **changes,
    }


def _poll(scanner, config, now, data=WEATHER, watermark="2026-07-15T00:00:00Z"):
    with (
        patch.object(scanner, "_fetch_weather", return_value=data),
        patch.object(scanner, "_now_utc", return_value=now),
        patch.object(_mod, "save_snapshot"),
    ):
        return scanner.poll(config, watermark)


REQUIRED_KEYS = {
    "id", "source", "type", "title", "preview", "discovered_at",
    "author", "author_name", "group", "url", "metadata",
}


def test_configure_includes_timezone_and_bounded_morning_window():
    config = _scanner().configure()
    assert config["enabled"] is False
    assert config["location"] == ""
    assert config["morning_hour"] == 8
    assert config["morning_window_hours"] == 4
    assert config["timezone"] == ""


def test_missing_location_and_fetch_failure_preserve_watermark():
    scanner = _scanner()
    assert scanner.poll({"location": ""}, "safe") == ([], "safe")
    with patch.object(scanner, "_fetch_weather", return_value=None):
        assert scanner.poll({"location": "London"}, "") == ([], "")


def test_first_poll_is_quiet_and_marks_current_morning_without_backfill():
    scanner = _scanner()
    now = datetime(2026, 7, 15, 8, 15, tzinfo=timezone.utc)
    pollen, watermark = _poll(scanner, _config(scanner), now, watermark="")
    assert pollen == []
    assert scanner._snapshot["bootstrap_pending"] is True
    assert "2026-07-15" in scanner._snapshot["candidate"]["morning_dates"]

    pollen, _ = _poll(scanner, _config(scanner), now, watermark=watermark)
    assert not any(item["type"] == "weather_morning" for item in pollen)


def test_morning_briefing_emits_in_configured_local_window():
    scanner = _scanner(ready=True)
    now = datetime(2026, 7, 15, 8, 15, tzinfo=timezone.utc)
    pollen, _ = _poll(scanner, _config(scanner), now)
    morning = next(item for item in pollen if item["type"] == "weather_morning")
    assert morning["title"] == "Weather: 22°C, Partly Cloudy"
    assert morning["metadata"]["location"] == "London"
    assert morning["id"].startswith("weather-morning-")
    assert REQUIRED_KEYS <= morning.keys()


def test_morning_briefing_does_not_repeat_same_local_day():
    committed = {
        "scope": _scope(),
        "last_temp": 22,
        "last_desc": "Partly Cloudy",
        "morning_dates": {"2026-07-15": "2026-07-15T08:00:00Z"},
    }
    scanner = _scanner(ready=True, committed=committed)
    now = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)
    pollen, _ = _poll(scanner, _config(scanner), now)
    assert not any(item["type"] == "weather_morning" for item in pollen)


def test_temperature_swing_and_precip_transition_are_reported():
    committed = {
        "scope": _scope(),
        "last_temp": 10,
        "last_desc": "Sunny",
        "morning_dates": {"2026-07-15": "2026-07-15T08:00:00Z"},
    }
    scanner = _scanner(ready=True, committed=committed)
    rainy = {
        **WEATHER,
        "current_condition": [{
            **WEATHER["current_condition"][0],
            "weatherDesc": [{"value": "Rain showers"}],
        }],
    }
    now = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    pollen, _ = _poll(scanner, _config(scanner), now, data=rainy)
    alerts = [item for item in pollen if item["type"] == "weather_alert"]
    assert len(alerts) == 2
    assert any("rose 12°C" in item["title"] for item in alerts)
    assert any(item["metadata"].get("current_desc") == "Rain showers" for item in alerts)


def test_precipitation_matching_uses_words_not_substrings():
    committed = {
        "scope": _scope(),
        "last_temp": 22,
        "last_desc": "Sunny",
        "morning_dates": {"2026-07-15": "2026-07-15T08:00:00Z"},
    }
    scanner = _scanner(ready=True, committed=committed)
    brainstorm = {
        **WEATHER,
        "current_condition": [{
            **WEATHER["current_condition"][0],
            "weatherDesc": [{"value": "Brainstorm clouds"}],
        }],
    }
    now = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    pollen, _ = _poll(scanner, _config(scanner), now, data=brainstorm)
    assert pollen == []


def test_location_or_timezone_scope_change_bootstraps_quietly():
    scanner = _scanner(ready=True)
    now = datetime(2026, 7, 15, 8, 15, tzinfo=timezone.utc)
    config = _config(scanner, location="Paris", alert_temp_swing_c=0)
    pollen, _ = _poll(scanner, config, now)
    assert pollen == []
    assert scanner._snapshot["candidate"]["scope"] != _scope()


def test_invalid_timezone_or_implausible_temperature_fails_closed():
    scanner = _scanner(ready=True)
    now = datetime(2026, 7, 15, 8, 15, tzinfo=timezone.utc)
    assert _poll(scanner, _config(scanner, timezone="Not/AZone"), now) == ([], "2026-07-15T00:00:00Z")
    impossible = {
        **WEATHER,
        "current_condition": [{**WEATHER["current_condition"][0], "temp_C": "999"}],
    }
    assert _poll(scanner, _config(scanner), now, data=impossible) == ([], "2026-07-15T00:00:00Z")


def test_invalid_config_is_rejected_before_any_network_request():
    scanner = _scanner(ready=True)
    watermark = "2026-07-15T00:00:00Z"
    with patch.object(scanner, "_fetch_weather") as fetch:
        assert scanner.poll(_config(scanner, morning_hour=True), watermark) == ([], watermark)
        assert scanner.poll(_config(scanner, alert_temp_swing_c=float("nan")), watermark) == ([], watermark)
        assert scanner.poll(_config(scanner, location=" London"), watermark) == ([], watermark)
    fetch.assert_not_called()


def test_malformed_provider_fields_fail_closed_without_advancing_state():
    scanner = _scanner(ready=True)
    now = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    watermark = "2026-07-15T00:00:00Z"
    bad_humidity = {
        **WEATHER,
        "current_condition": [{**WEATHER["current_condition"][0], "humidity": "unknown"}],
    }
    assert _poll(scanner, _config(scanner), now, data=bad_humidity, watermark=watermark) == (
        [], watermark,
    )
    bad_forecast = {**WEATHER, "weather": [{"maxtempC": {}, "mintempC": "15"}]}
    assert _poll(scanner, _config(scanner), now, data=bad_forecast, watermark=watermark) == (
        [], watermark,
    )


def test_corrupt_transaction_snapshot_fails_closed():
    scanner = _scanner(ready=True)
    scanner._snapshot["committed"]["morning_dates"] = {"not-a-date": "value"}
    watermark = "2026-07-15T00:00:00Z"
    with patch.object(scanner, "_fetch_weather") as fetch:
        assert scanner.poll(_config(scanner), watermark) == ([], watermark)
    fetch.assert_not_called()


def test_unknown_snapshot_version_and_malformed_candidate_fail_closed():
    watermark = "2026-07-15T00:00:00Z"
    scanner = _scanner(ready=True)
    scanner._snapshot = {"schema_version": 99}
    with patch.object(scanner, "_fetch_weather") as fetch:
        assert scanner.poll(_config(scanner), watermark) == ([], watermark)
    fetch.assert_not_called()

    scanner = _scanner(ready=True)
    scanner._snapshot["candidate"]["last_temp"] = "22"
    with patch.object(scanner, "_fetch_weather") as fetch:
        assert scanner.poll(_config(scanner), watermark) == ([], watermark)
    fetch.assert_not_called()


def test_recognized_legacy_snapshot_migrates_without_emitting_backlog():
    scanner = _scanner(ready=True)
    scanner._snapshot = {
        "last_temp": 5,
        "last_desc": "Sunny",
        "morning_briefing_2026-07-15": "2026-07-15T08:00:00Z",
    }
    now = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)
    pollen, _ = _poll(scanner, _config(scanner), now)
    assert pollen == []
    assert scanner._snapshot["schema_version"] == 2
    assert scanner._snapshot["bootstrap_pending"] is True


def test_zero_temperature_threshold_only_alerts_on_an_actual_change():
    scanner = _scanner(ready=True)
    now = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    pollen, _ = _poll(scanner, _config(scanner, alert_temp_swing_c=0), now)
    assert not any(item["type"] == "weather_alert" for item in pollen)


def test_inverted_or_oversized_provider_forecast_fails_closed():
    scanner = _scanner(ready=True)
    now = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    watermark = "2026-07-15T00:00:00Z"
    inverted = {**WEATHER, "weather": [{"maxtempC": "10", "mintempC": "20"}]}
    assert _poll(scanner, _config(scanner), now, inverted, watermark) == ([], watermark)
    oversized = {**WEATHER, "weather": WEATHER["weather"] * 101}
    assert _poll(scanner, _config(scanner), now, oversized, watermark) == ([], watermark)


def test_weather_redirects_cannot_leave_the_https_wttr_origin():
    handler = _mod._SameOriginRedirectHandler()
    request = Request("https://wttr.in/London?format=j1")
    with pytest.raises(HTTPError):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://127.0.0.1/internal",
        )


def test_weather_json_rejects_duplicate_keys_and_nonfinite_numbers():
    assert WeatherScanner._strict_json('{"weather":[],"weather":[]}') is None
    assert WeatherScanner._strict_json('{"value":NaN}') is None
