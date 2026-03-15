"""Weather scanner — surfaces weather briefings and alerts via wttr.in."""

import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone

# Resolve imports whether run as module or standalone
try:
    from snapshot_store import load_snapshot, save_snapshot
except ImportError:
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from snapshot_store import load_snapshot, save_snapshot

_RAIN_SNOW_TERMS = {"rain", "snow", "sleet", "drizzle", "shower", "thunderstorm", "blizzard"}


class WeatherScanner:
    name = "weather"

    def __init__(self):
        self._snapshot = load_snapshot("weather_conditions")

    def configure(self) -> dict:
        return {
            "enabled": False,
            "location": "",
            "morning_hour": 8,
            "alert_temp_swing_c": 10,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def _fetch_weather(self, location: str) -> dict | None:
        """Fetch weather data from wttr.in, return parsed JSON or None."""
        url = f"https://wttr.in/{location}?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "HiveScanner/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            print(f"[weather] fetch failed: {e}", file=sys.stderr)
            return None

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        location = config.get("location", "")
        if not location:
            return [], watermark

        data = self._fetch_weather(location)
        if data is None:
            return [], watermark

        current_conditions = data.get("current_condition", [])
        if not current_conditions:
            return [], watermark

        current = current_conditions[0]
        current_temp = int(current.get("temp_C", 0))
        humidity = current.get("humidity", "")
        weather_descs = current.get("weatherDesc", [])
        weather_desc = weather_descs[0].get("value", "") if weather_descs else ""

        forecast = data.get("weather", [])
        forecast_preview = ""
        if forecast:
            today = forecast[0]
            max_temp = today.get("maxtempC", "")
            min_temp = today.get("mintempC", "")
            forecast_preview = f"High: {max_temp}°C, Low: {min_temp}°C"

        now = self._now_utc()
        today_str = now.strftime("%Y-%m-%d")
        alert_temp_swing_c = config.get("alert_temp_swing_c", 10)

        items = []

        # --- Temperature swing alert ---
        last_temp = self._snapshot.get("last_temp")
        if last_temp is not None:
            swing = abs(current_temp - last_temp)
            if swing >= alert_temp_swing_c:
                direction = "rose" if current_temp > last_temp else "dropped"
                alert_hash = hashlib.md5(
                    f"{current_temp}-{last_temp}-{today_str}".encode()
                ).hexdigest()[:8]
                items.append({
                    "id": f"weather-alert-{today_str}-{alert_hash}",
                    "source": "weather",
                    "type": "weather_alert",
                    "title": f"Weather Alert: Temperature {direction} {swing}°C",
                    "preview": f"Temperature {direction} from {last_temp}°C to {current_temp}°C. {forecast_preview}",
                    "discovered_at": self._utc_now_z(),
                    "author": "wttr.in",
                    "author_name": "wttr.in",
                    "group": "Weather",
                    "url": f"https://wttr.in/{location}",
                    "metadata": {
                        "current_temp_c": current_temp,
                        "previous_temp_c": last_temp,
                        "swing_c": swing,
                        "location": location,
                    },
                })

        # --- Rain/snow starting alert ---
        last_desc = self._snapshot.get("last_desc", "")
        last_desc_lower = last_desc.lower()
        current_desc_lower = weather_desc.lower()
        had_precip = any(term in last_desc_lower for term in _RAIN_SNOW_TERMS)
        has_precip = any(term in current_desc_lower for term in _RAIN_SNOW_TERMS)
        if has_precip and not had_precip and last_desc:
            precip_hash = hashlib.md5(
                f"{weather_desc}-{today_str}".encode()
            ).hexdigest()[:8]
            items.append({
                "id": f"weather-alert-{today_str}-{precip_hash}",
                "source": "weather",
                "type": "weather_alert",
                "title": f"Weather Alert: {weather_desc}",
                "preview": f"Conditions changed from '{last_desc}' to '{weather_desc}'. {forecast_preview}",
                "discovered_at": self._utc_now_z(),
                "author": "wttr.in",
                "author_name": "wttr.in",
                "group": "Weather",
                "url": f"https://wttr.in/{location}",
                "metadata": {
                    "current_desc": weather_desc,
                    "previous_desc": last_desc,
                    "location": location,
                },
            })

        # --- Morning briefing ---
        morning_hour = config.get("morning_hour", 8)
        morning_key = f"morning_briefing_{today_str}"
        if now.hour == morning_hour and not self._snapshot.get(morning_key):
            items.append({
                "id": f"weather-morning-{today_str}",
                "source": "weather",
                "type": "weather_morning",
                "title": f"Weather: {current_temp}°C, {weather_desc}",
                "preview": f"Current: {current_temp}°C, {weather_desc}, Humidity: {humidity}%. {forecast_preview}",
                "discovered_at": self._utc_now_z(),
                "author": "wttr.in",
                "author_name": "wttr.in",
                "group": "Weather",
                "url": f"https://wttr.in/{location}",
                "metadata": {
                    "current_temp_c": current_temp,
                    "humidity": humidity,
                    "weather_desc": weather_desc,
                    "location": location,
                },
            })
            self._snapshot[morning_key] = self._utc_now_z()

        # Update snapshot
        self._snapshot["last_temp"] = current_temp
        self._snapshot["last_desc"] = weather_desc
        save_snapshot("weather_conditions", self._snapshot)

        return items, self._utc_now_z()
