"""Weather scanner — surfaces weather briefings and alerts via wttr.in."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Resolve imports whether run as module or standalone
try:
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists
except ImportError:
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from snapshot_store import load_snapshot, save_snapshot, snapshot_exists

_RAIN_SNOW_TERMS = {"rain", "snow", "sleet", "drizzle", "shower", "thunderstorm", "blizzard"}


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow wttr.in redirects only when they remain HTTPS and same-origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.hostname != "wttr.in" or target.port not in {None, 443}:
            raise HTTPError(newurl, code, "cross-origin weather redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class WeatherScanner:
    name = "weather"

    def __init__(self):
        self._snapshot = load_snapshot("weather_conditions")
        self._bootstrapped = snapshot_exists("weather_conditions")

    def configure(self) -> dict:
        return {
            "enabled": False,
            "location": "",
            "morning_hour": 8,
            "timezone": "",
            "morning_window_hours": 4,
            "alert_temp_swing_c": 10,
        }

    def _utc_now_z(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def _fetch_weather(self, location: str) -> dict | None:
        """Fetch weather data from wttr.in, return parsed JSON or None."""
        encoded_location = urllib.parse.quote(location.strip(), safe="")
        url = f"https://wttr.in/{encoded_location}?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "HiveScanner/1.0"})
        try:
            opener = urllib.request.build_opener(_SameOriginRedirectHandler())
            with opener.open(req, timeout=10) as resp:
                raw = resp.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("response exceeded 1 MB")
                data = self._strict_json(raw.decode("utf-8"))
                return data if isinstance(data, dict) else None
        except (OSError, ValueError, UnicodeDecodeError) as e:
            print(f"[weather] fetch failed: {e}", file=sys.stderr)
            return None

    @staticmethod
    def _strict_json(raw: str) -> object | None:
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
            return json.loads(
                raw,
                parse_constant=reject_constant,
                object_pairs_hook=strict_object,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 64
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _candidate_committed(cls, watermark: str, candidate_watermark: str) -> bool:
        current = cls._parse_timestamp(watermark)
        candidate = cls._parse_timestamp(candidate_watermark)
        return current is not None and candidate is not None and current >= candidate

    @classmethod
    def _valid_state(cls, value: object) -> dict | None:
        if not isinstance(value, dict) or len(value) > 40:
            return None
        allowed_keys = {"scope", "last_temp", "last_desc", "morning_dates"}
        if not set(value).issubset(allowed_keys):
            return None
        scope = value.get("scope")
        last_temp = value.get("last_temp")
        last_desc = value.get("last_desc")
        morning_dates = value.get("morning_dates", {})
        if scope is not None and (
            not isinstance(scope, str) or re.fullmatch(r"[0-9a-f]{16}", scope) is None
        ):
            return None
        if last_temp is not None and (
            isinstance(last_temp, bool)
            or not isinstance(last_temp, int)
            or not -100 <= last_temp <= 100
        ):
            return None
        if last_desc is not None and (
            not isinstance(last_desc, str)
            or len(last_desc) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in last_desc)
        ):
            return None
        if not isinstance(morning_dates, dict) or len(morning_dates) > 31:
            return None
        normalized_dates = {}
        for day, observed_at in morning_dates.items():
            if (
                not isinstance(day, str)
                or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None
                or not isinstance(observed_at, str)
                or len(observed_at) > 64
                or any(ord(char) < 32 or ord(char) == 127 for char in observed_at)
                or cls._parse_timestamp(observed_at) is None
            ):
                return None
            try:
                parsed_day = datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                return None
            if parsed_day.strftime("%Y-%m-%d") != day:
                return None
            normalized_dates[day] = observed_at
        return {
            **({"scope": scope} if scope is not None else {}),
            **({"last_temp": last_temp} if last_temp is not None else {}),
            **({"last_desc": last_desc} if last_desc is not None else {}),
            "morning_dates": normalized_dates,
        }

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        if not isinstance(config, dict) or not isinstance(watermark, str):
            return [], watermark
        if watermark and self._parse_timestamp(watermark) is None:
            print("[weather] invalid watermark; preserving it", file=sys.stderr)
            return [], watermark
        raw_location = config.get("location", "")
        raw_timezone = config.get("timezone", "")
        if not isinstance(raw_location, str) or raw_location != raw_location.strip():
            return [], watermark
        if not isinstance(raw_timezone, str) or raw_timezone != raw_timezone.strip():
            return [], watermark
        location = raw_location
        timezone_name = raw_timezone
        if (
            not location
            or len(location) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in location)
            or len(timezone_name) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in timezone_name)
        ):
            return [], watermark

        morning_hour = config.get("morning_hour", 8)
        morning_window = config.get("morning_window_hours", 4)
        alert_temp_swing_c = config.get("alert_temp_swing_c", 10)
        if (
            isinstance(morning_hour, bool)
            or not isinstance(morning_hour, int)
            or not 0 <= morning_hour <= 23
            or isinstance(morning_window, bool)
            or not isinstance(morning_window, int)
            or not 1 <= morning_window <= 12
            or isinstance(alert_temp_swing_c, bool)
            or not isinstance(alert_temp_swing_c, (int, float))
            or not math.isfinite(alert_temp_swing_c)
            or not 0 <= alert_temp_swing_c <= 100
        ):
            print("[weather] invalid scanner configuration", file=sys.stderr)
            return [], watermark
        alert_temp_swing_c = float(alert_temp_swing_c)

        try:
            local_zone = ZoneInfo(timezone_name) if timezone_name else datetime.now().astimezone().tzinfo
        except ZoneInfoNotFoundError:
            print(f"[weather] unknown timezone: {timezone_name}", file=sys.stderr)
            return [], watermark

        bootstrap_pending = False
        if self._snapshot.get("schema_version") == 2:
            committed = self._valid_state(self._snapshot.get("committed"))
            candidate = self._valid_state(self._snapshot.get("candidate"))
            candidate_wm = self._snapshot.get("candidate_watermark")
            bootstrap_pending = self._snapshot.get("bootstrap_pending")
            if (
                type(self._snapshot.get("schema_version")) is not int
                or set(self._snapshot)
                != {
                    "schema_version",
                    "committed",
                    "candidate",
                    "candidate_watermark",
                    "bootstrap_pending",
                }
                or committed is None
                or candidate is None
                or not isinstance(candidate_wm, str)
                or self._parse_timestamp(candidate_wm) is None
                or not isinstance(bootstrap_pending, bool)
            ):
                print("[weather] invalid persisted snapshot; preserving watermark", file=sys.stderr)
                return [], watermark
            if candidate_wm and self._candidate_committed(watermark, candidate_wm):
                committed = candidate
            if (
                bootstrap_pending
                and candidate_wm
                and self._candidate_committed(watermark, candidate_wm)
            ):
                bootstrap_pending = False
        else:
            if "schema_version" in self._snapshot:
                print("[weather] unsupported snapshot version", file=sys.stderr)
                return [], watermark
            allowed_legacy_keys = {"scope", "last_temp", "last_desc", "morning_dates"}
            if any(
                not isinstance(key, str)
                or (
                    key not in allowed_legacy_keys
                    and not key.startswith("morning_briefing_")
                )
                for key in self._snapshot
            ) or (not self._bootstrapped and self._snapshot):
                print("[weather] invalid legacy snapshot; preserving watermark", file=sys.stderr)
                return [], watermark
            legacy = dict(self._snapshot)
            legacy_mornings = {
                key.removeprefix("morning_briefing_"): value
                for key, value in legacy.items()
                if isinstance(key, str) and key.startswith("morning_briefing_")
            }
            legacy["morning_dates"] = legacy_mornings
            committed = self._valid_state({
                key: value
                for key, value in legacy.items()
                if key in {"scope", "last_temp", "last_desc", "morning_dates"}
            })
            if committed is None:
                print("[weather] invalid legacy snapshot; preserving watermark", file=sys.stderr)
                return [], watermark
            bootstrap_pending = self._bootstrapped

        encoded_location = urllib.parse.quote(location, safe="")

        data = self._fetch_weather(location)
        if data is None:
            return [], watermark

        current_conditions = data.get("current_condition", [])
        if (
            not isinstance(current_conditions, list)
            or not current_conditions
            or len(current_conditions) > 100
        ):
            return [], watermark

        current = current_conditions[0]
        if not isinstance(current, dict):
            return [], watermark
        raw_temp = current.get("temp_C")
        raw_humidity = current.get("humidity")
        weather_descs = current.get("weatherDesc")
        if (
            not isinstance(raw_temp, str)
            or re.fullmatch(r"-?\d{1,3}", raw_temp) is None
            or not isinstance(raw_humidity, str)
            or re.fullmatch(r"\d{1,3}", raw_humidity) is None
            or not isinstance(weather_descs, list)
            or not weather_descs
            or len(weather_descs) > 100
            or not isinstance(weather_descs[0], dict)
            or not isinstance(weather_descs[0].get("value"), str)
        ):
            print("[weather] response has no valid current temperature", file=sys.stderr)
            return [], watermark
        current_temp = int(raw_temp)
        humidity_value = int(raw_humidity)
        weather_desc = weather_descs[0]["value"]
        if (
            not -100 <= current_temp <= 100
            or not 0 <= humidity_value <= 100
            or not weather_desc
            or len(weather_desc) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in weather_desc)
        ):
            print("[weather] current conditions are outside plausible bounds", file=sys.stderr)
            return [], watermark
        humidity = raw_humidity

        forecast = data.get("weather")
        if (
            not isinstance(forecast, list)
            or not forecast
            or len(forecast) > 100
            or not isinstance(forecast[0], dict)
        ):
            return [], watermark
        today = forecast[0]
        max_temp = today.get("maxtempC")
        min_temp = today.get("mintempC")
        if (
            not isinstance(max_temp, str)
            or re.fullmatch(r"-?\d{1,3}", max_temp) is None
            or not isinstance(min_temp, str)
            or re.fullmatch(r"-?\d{1,3}", min_temp) is None
            or not -100 <= int(max_temp) <= 100
            or not -100 <= int(min_temp) <= 100
            or int(max_temp) < int(min_temp)
        ):
            return [], watermark
        forecast_preview = f"High: {max_temp}°C, Low: {min_temp}°C"

        now_utc = self._now_utc()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        else:
            now_utc = now_utc.astimezone(timezone.utc)
        scan_started_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        now = now_utc.astimezone(local_zone)
        today_str = now.strftime("%Y-%m-%d")

        # `datetime.now().astimezone().tzinfo` is commonly a fixed-offset
        # object whose display name flips between PST/PDT.  Treat an omitted
        # timezone as one stable system-local scope so DST does not trigger a
        # false configuration change and silent re-bootstrap.
        resolved_timezone = (
            getattr(local_zone, "key", str(local_zone))
            if timezone_name
            else "system-local"
        )
        scope = hashlib.sha256(
            f"{location.casefold()}\0{resolved_timezone}".encode()
        ).hexdigest()[:16]
        scope_changed = bool(committed) and committed.get("scope") not in {None, scope}
        if scope_changed:
            committed = {}
        is_bootstrap = not self._bootstrapped or bootstrap_pending or scope_changed
        items = []

        # --- Temperature swing alert ---
        last_temp = committed.get("last_temp")
        if last_temp is not None and not is_bootstrap:
            try:
                previous_temp = int(last_temp)
            except (TypeError, ValueError):
                previous_temp = current_temp
            swing = abs(current_temp - previous_temp)
            if swing > 0 and swing >= alert_temp_swing_c:
                direction = "rose" if current_temp > previous_temp else "dropped"
                alert_hash = hashlib.sha256(
                    f"{scope}-{current_temp}-{previous_temp}-{today_str}".encode()
                ).hexdigest()[:16]
                items.append({
                    "id": f"weather-alert-{today_str}-{alert_hash}",
                    "source": "weather",
                    "type": "weather_alert",
                    "title": f"Weather Alert: Temperature {direction} {swing}°C",
                    "preview": f"Temperature {direction} from {previous_temp}°C to {current_temp}°C. {forecast_preview}",
                    "discovered_at": scan_started_at,
                    "author": "wttr.in",
                    "author_name": "wttr.in",
                    "group": "Weather",
                    "url": f"https://wttr.in/{encoded_location}",
                    "metadata": {
                        "current_temp_c": current_temp,
                        "previous_temp_c": previous_temp,
                        "swing_c": swing,
                        "location": location,
                    },
                })

        # --- Rain/snow starting alert ---
        last_desc = str(committed.get("last_desc") or "")
        last_desc_lower = last_desc.lower()
        current_desc_lower = weather_desc.lower()
        had_precip = any(
            re.search(rf"\b{re.escape(term)}\b", last_desc_lower)
            for term in _RAIN_SNOW_TERMS
        )
        has_precip = any(
            re.search(rf"\b{re.escape(term)}\b", current_desc_lower)
            for term in _RAIN_SNOW_TERMS
        )
        if has_precip and not had_precip and last_desc and not is_bootstrap:
            precip_hash = hashlib.sha256(
                f"{scope}-{weather_desc}-{today_str}".encode()
            ).hexdigest()[:16]
            items.append({
                "id": f"weather-alert-{today_str}-{precip_hash}",
                "source": "weather",
                "type": "weather_alert",
                "title": f"Weather Alert: {weather_desc}",
                "preview": f"Conditions changed from '{last_desc}' to '{weather_desc}'. {forecast_preview}",
                "discovered_at": scan_started_at,
                "author": "wttr.in",
                "author_name": "wttr.in",
                "group": "Weather",
                "url": f"https://wttr.in/{encoded_location}",
                "metadata": {
                    "current_desc": weather_desc,
                    "previous_desc": last_desc,
                    "location": location,
                },
            })

        # --- Morning briefing ---
        morning_start = now.replace(
            hour=morning_hour, minute=0, second=0, microsecond=0
        )
        if now < morning_start:
            morning_start -= timedelta(days=1)
        in_morning_window = now < morning_start + timedelta(hours=morning_window)
        morning_day = morning_start.strftime("%Y-%m-%d")
        morning_dates = committed.get("morning_dates", {})
        candidate_mornings = dict(morning_dates)
        if in_morning_window and morning_day not in morning_dates:
            if not is_bootstrap:
                items.append({
                    "id": f"weather-morning-{scope}-{morning_day}",
                    "source": "weather",
                    "type": "weather_morning",
                    "title": f"Weather: {current_temp}°C, {weather_desc}",
                    "preview": f"Current: {current_temp}°C, {weather_desc}, Humidity: {humidity}%. {forecast_preview}",
                    "discovered_at": scan_started_at,
                    "author": "wttr.in",
                    "author_name": "wttr.in",
                    "group": "Weather",
                    "url": f"https://wttr.in/{encoded_location}",
                    "metadata": {
                        "current_temp_c": current_temp,
                        "humidity": humidity,
                        "weather_desc": weather_desc,
                        "location": location,
                    },
                })
            candidate_mornings[morning_day] = scan_started_at

        candidate_mornings = dict(sorted(candidate_mornings.items())[-14:])
        candidate_state = {
            "scope": scope,
            "last_temp": current_temp,
            "last_desc": weather_desc,
            "morning_dates": candidate_mornings,
        }
        self._snapshot = {
            "schema_version": 2,
            "committed": committed,
            "candidate": candidate_state,
            "candidate_watermark": scan_started_at,
            "bootstrap_pending": is_bootstrap,
        }
        save_snapshot("weather_conditions", self._snapshot)
        self._bootstrapped = True

        return items, scan_started_at
