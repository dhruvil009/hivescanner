"""Tests for weather scanner."""

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

# Load weather.py by absolute path under the name "weather" so existing
# @patch("weather.X") decorators resolve. Add workers/ (NOT workers/sources/)
# to sys.path so the scanner's sibling imports (dep_installer, snapshot_store)
# resolve. Never add workers/sources/ — it shadows stdlib email/calendar.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
_WEATHER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "workers", "sources", "weather.py"
)
_spec = importlib.util.spec_from_file_location("weather", _WEATHER_PATH)
_weather_mod = importlib.util.module_from_spec(_spec)
sys.modules["weather"] = _weather_mod
_spec.loader.exec_module(_weather_mod)
WeatherScanner = _weather_mod.WeatherScanner


SAMPLE_WTTR_RESPONSE = {
    "current_condition": [
        {
            "temp_C": "22",
            "humidity": "55",
            "weatherDesc": [{"value": "Partly Cloudy"}],
        }
    ],
    "weather": [
        {
            "maxtempC": "28",
            "mintempC": "15",
        }
    ],
}

REQUIRED_POLLEN_KEYS = {
    "id", "source", "type", "title", "preview",
    "discovered_at", "author", "author_name",
    "group", "url", "metadata",
}


def _make_urlopen_response(data: dict):
    """Create a mock urlopen response context manager."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode()
    mock_resp.__enter__ = lambda self: self
    mock_resp.__exit__ = lambda self, *a: None
    return mock_resp


@pytest.fixture
def scanner():
    with patch("weather.load_snapshot", return_value={}), \
         patch("weather.save_snapshot"):
        return WeatherScanner()


class TestWeatherScanner:

    def test_configure_returns_expected_defaults(self, scanner):
        config = scanner.configure()
        assert config["enabled"] is False
        assert config["location"] == ""
        assert config["morning_hour"] == 8
        assert config["alert_temp_swing_c"] == 10

    def test_poll_returns_empty_when_no_location(self, scanner):
        with patch("weather.save_snapshot"):
            pollen, wm = scanner.poll({"location": ""}, "")
        assert pollen == []

    def test_morning_briefing_emitted_at_correct_hour(self, scanner):
        mock_resp = _make_urlopen_response(SAMPLE_WTTR_RESPONSE)
        morning_hour = 8
        fake_now = datetime(2026, 3, 15, morning_hour, 15, 0, tzinfo=timezone.utc)

        with patch("weather.save_snapshot"), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(scanner, "_now_utc", return_value=fake_now):
            config = {"location": "London", "morning_hour": morning_hour, "alert_temp_swing_c": 10}
            pollen, wm = scanner.poll(config, "")

        morning = [p for p in pollen if p["type"] == "weather_morning"]
        assert len(morning) == 1
        assert morning[0]["id"] == "weather-morning-2026-03-15"
        assert "22°C" in morning[0]["title"]

    def test_weather_alert_on_temperature_swing(self, scanner):
        # Seed snapshot with a previous temp that differs by >= swing threshold
        scanner._snapshot = {"last_temp": 10, "last_desc": "Sunny"}
        mock_resp = _make_urlopen_response(SAMPLE_WTTR_RESPONSE)  # current is 22
        fake_now = datetime(2026, 3, 15, 14, 0, 0, tzinfo=timezone.utc)

        with patch("weather.save_snapshot"), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(scanner, "_now_utc", return_value=fake_now):
            config = {"location": "London", "morning_hour": 8, "alert_temp_swing_c": 10}
            pollen, wm = scanner.poll(config, "")

        alerts = [p for p in pollen if p["type"] == "weather_alert"]
        assert len(alerts) == 1
        assert "12°C" in alerts[0]["title"]
        assert alerts[0]["id"].startswith("weather-alert-2026-03-15-")

    def test_no_duplicate_morning_briefing_same_day(self, scanner):
        morning_hour = 8
        fake_now = datetime(2026, 3, 15, morning_hour, 30, 0, tzinfo=timezone.utc)
        # Simulate already-emitted morning briefing by pre-setting the key
        scanner._snapshot = {"morning_briefing_2026-03-15": "2026-03-15T08:00:00Z"}

        mock_resp = _make_urlopen_response(SAMPLE_WTTR_RESPONSE)

        with patch("weather.save_snapshot"), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(scanner, "_now_utc", return_value=fake_now):
            config = {"location": "London", "morning_hour": morning_hour, "alert_temp_swing_c": 10}
            pollen, wm = scanner.poll(config, "")

        morning = [p for p in pollen if p["type"] == "weather_morning"]
        assert len(morning) == 0

    def test_pollen_schema_has_all_required_keys(self, scanner):
        scanner._snapshot = {"last_temp": 10, "last_desc": "Sunny"}
        mock_resp = _make_urlopen_response(SAMPLE_WTTR_RESPONSE)
        fake_now = datetime(2026, 3, 15, 8, 0, 0, tzinfo=timezone.utc)

        with patch("weather.save_snapshot"), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(scanner, "_now_utc", return_value=fake_now):
            config = {"location": "London", "morning_hour": 8, "alert_temp_swing_c": 10}
            pollen, wm = scanner.poll(config, "")

        assert len(pollen) > 0, "Expected at least one pollen item"
        for item in pollen:
            missing = REQUIRED_POLLEN_KEYS - set(item.keys())
            assert not missing, f"Pollen missing keys: {missing}"
