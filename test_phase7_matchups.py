"""
Open-Meteo adapter. Free, no key. Forecast up to 16 days hourly; archive for completed games (backtest).

  forecast: https://api.open-meteo.com/v1/forecast
  archive : https://archive-api.open-meteo.com/v1/archive
Units requested: Fahrenheit, mph, inches, UTC hourly timestamps.

A game's weather row = the hourly values at the kickoff hour (UTC, floored). Indoor venues (dome, or a
retractable roof recorded as closed) get an is_indoor=True row with all weather fields NULL, by rule.
Retractable roofs with unknown status are fetched and flagged roof_status_unknown=True.
"""
from __future__ import annotations
from datetime import datetime

import pandas as pd

from providers.base import RequestManager

FORECAST = "https://api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation_probability,precipitation,wind_speed_10m,wind_gusts_10m,wind_direction_10m,weather_code"
HOURLY_ARCHIVE = HOURLY.replace("precipitation_probability,", "")
UNITS = {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch", "timezone": "UTC"}

INDOOR_ROOFS = {"dome", "retractable_closed"}


def fetch_forecast(rm: RequestManager, lat: float, lon: float, days: int = 16):
    return rm.get(FORECAST, params={"latitude": lat, "longitude": lon, "hourly": HOURLY, "forecast_days": days, **UNITS})


def fetch_archive(rm: RequestManager, lat: float, lon: float, day: str):
    return rm.get(ARCHIVE, params={"latitude": lat, "longitude": lon, "hourly": HOURLY_ARCHIVE, "start_date": day, "end_date": day, **UNITS})


def pick_hour(payload: dict, kickoff_utc: pd.Timestamp) -> dict | None:
    """Return the hourly record at the kickoff hour, or None if not covered."""
    h = payload.get("hourly") or {}
    times = h.get("time") or []
    key = kickoff_utc.floor("h").strftime("%Y-%m-%dT%H:%M")
    if key not in times:
        return None
    i = times.index(key)

    def v(name):
        arr = h.get(name)
        return arr[i] if arr and i < len(arr) else None

    return {"temp_f": v("temperature_2m"), "feels_like_f": v("apparent_temperature"), "humidity_pct": v("relative_humidity_2m"),
            "precip_prob": (v("precipitation_probability") / 100.0) if v("precipitation_probability") is not None else None,
            "precip_in": v("precipitation"), "wind_mph": v("wind_speed_10m"), "wind_gust_mph": v("wind_gusts_10m"),
            "wind_dir_deg": v("wind_direction_10m"), "condition_code": v("weather_code")}


def snapshot_row(game_id: str, kickoff_utc: pd.Timestamp, roof: str | None, values: dict | None, retrieved_at: datetime) -> dict:
    indoor = roof in INDOOR_ROOFS
    base = {"game_id": game_id, "retrieved_at": retrieved_at.isoformat(), "forecast_for_utc": kickoff_utc.isoformat(),
            "hours_to_kickoff": round((kickoff_utc - pd.Timestamp(retrieved_at)).total_seconds() / 3600, 1),
            "is_indoor": indoor, "roof_status_unknown": roof == "retractable", "source": "open_meteo"}
    empty = {k: None for k in ("temp_f", "feels_like_f", "wind_mph", "wind_gust_mph", "wind_dir_deg", "precip_prob", "precip_in", "humidity_pct", "condition_code")}
    if indoor or values is None:
        return {**base, **empty}
    return {**base, **empty, **values}
