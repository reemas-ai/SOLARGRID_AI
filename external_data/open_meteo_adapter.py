"""Open-Meteo weather/solar forecast adapter.

Open-Meteo context is supporting evidence only.  No irradiance-to-MW conversion
is performed here or anywhere in the ingestion layer.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import requests

from .provenance import ExternalPayload
from .quality import freshness_status, parse_time, plausible_range, required_fields, utc_now

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
DEFAULT_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def geocode(place_name: str, *, timeout_seconds: float = 8.0) -> dict[str, Any]:
    response = requests.get(
        GEOCODING_URL,
        params={"name": place_name, "count": 1, "language": "en", "format": "json"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    rows = response.json().get("results") or []
    if not rows:
        raise ValueError(f"Open-Meteo geocoding returned no result for {place_name!r}")
    row = rows[0]
    return {
        "name": row.get("name") or place_name,
        "country": row.get("country"),
        "latitude": float(row["latitude"]),
        "longitude": float(row["longitude"]),
    }


def fetch_forecast(
    *,
    place_name: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    forecast_hours: int = 6,
    freshness_minutes: int = 30,
    timeout_seconds: float = 10.0,
) -> ExternalPayload:
    retrieved = utc_now()
    location_label = place_name
    try:
        if latitude is None or longitude is None:
            if not place_name:
                raise ValueError("place_name or latitude/longitude is required")
            location = geocode(place_name, timeout_seconds=timeout_seconds)
            latitude = location["latitude"]
            longitude = location["longitude"]
            location_label = ", ".join(x for x in [location.get("name"), location.get("country")] if x)

        base_url = os.getenv("OPEN_METEO_BASE_URL", DEFAULT_FORECAST_URL).strip() or DEFAULT_FORECAST_URL
        hourly_vars = "temperature_2m,cloud_cover,shortwave_radiation,direct_normal_irradiance"
        response = requests.get(
            base_url,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,cloud_cover,shortwave_radiation,direct_normal_irradiance",
                "hourly": hourly_vars,
                "forecast_hours": max(1, int(forecast_hours)),
                "timezone": "UTC",
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        raw = response.json()
        current = raw.get("current") or {}
        ok, missing = required_fields(current, ["time", "temperature_2m", "cloud_cover", "shortwave_radiation"])
        observed = parse_time(current.get("time"))
        status = freshness_status(observed, max_age_minutes=freshness_minutes)
        if not ok:
            status = "INCOMPLETE"
        if current.get("cloud_cover") is not None and not plausible_range(current.get("cloud_cover"), 0, 100):
            status = "INVALID"
        if current.get("shortwave_radiation") is not None and not plausible_range(current.get("shortwave_radiation"), 0, 1600):
            status = "INVALID"

        payload = {
            "latitude": latitude,
            "longitude": longitude,
            "current": current,
            "hourly": raw.get("hourly") or {},
            "hourly_units": raw.get("hourly_units") or {},
            "current_units": raw.get("current_units") or {},
            "missing_required_fields": missing,
        }
        return ExternalPayload(
            source="OPEN_METEO",
            source_role="REAL_OPEN_API_WEATHER_SOLAR_CONTEXT",
            status=status,
            retrieved_at=retrieved,
            observed_at=observed,
            location=location_label or f"{latitude},{longitude}",
            variables=["temperature_2m", "cloud_cover", "shortwave_radiation", "direct_normal_irradiance"],
            payload=payload,
            endpoint=base_url,
        )
    except Exception as exc:
        return ExternalPayload(
            source="OPEN_METEO",
            source_role="REAL_OPEN_API_WEATHER_SOLAR_CONTEXT",
            status="UNAVAILABLE",
            retrieved_at=retrieved,
            location=location_label,
            variables=["temperature_2m", "cloud_cover", "shortwave_radiation", "direct_normal_irradiance"],
            payload={},
            error=f"{type(exc).__name__}: {exc}",
            endpoint=os.getenv("OPEN_METEO_BASE_URL", DEFAULT_FORECAST_URL),
        )
