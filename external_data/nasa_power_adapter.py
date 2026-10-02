"""NASA POWER hourly historical solar-reference adapter."""
from __future__ import annotations

import os
from datetime import timedelta

import requests

from .provenance import ExternalPayload
from .quality import utc_now

DEFAULT_BASE = "https://power.larc.nasa.gov/api/"


def fetch_hourly_reference(
    *,
    latitude: float,
    longitude: float,
    lookback_days: int = 7,
    timeout_seconds: float = 15.0,
) -> ExternalPayload:
    retrieved = utc_now()
    target = (retrieved - timedelta(days=max(2, int(lookback_days)))).date()
    date_token = target.strftime("%Y%m%d")
    base = os.getenv("NASA_POWER_BASE_URL", DEFAULT_BASE).rstrip("/") + "/"
    endpoint = base + "temporal/hourly/point"
    try:
        response = requests.get(
            endpoint,
            params={
                "parameters": "T2M,ALLSKY_SFC_SW_DWN",
                "community": "RE",
                "longitude": longitude,
                "latitude": latitude,
                "start": date_token,
                "end": date_token,
                "format": "JSON",
                "time-standard": "UTC",
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        raw = response.json()
        parameters = (((raw.get("properties") or {}).get("parameter")) or {})
        if not isinstance(parameters, dict) or not parameters:
            raise ValueError("NASA POWER returned no hourly parameter data")
        return ExternalPayload(
            source="NASA_POWER",
            source_role="HISTORICAL_SOLAR_REFERENCE",
            status="REFERENCE",
            retrieved_at=retrieved,
            observed_at=None,
            location=f"{latitude},{longitude}",
            variables=["ALLSKY_SFC_SW_DWN", "T2M"],
            payload={"reference_date": date_token, "parameters": parameters},
            endpoint=endpoint,
        )
    except Exception as exc:
        return ExternalPayload(
            source="NASA_POWER",
            source_role="HISTORICAL_SOLAR_REFERENCE",
            status="UNAVAILABLE",
            retrieved_at=retrieved,
            location=f"{latitude},{longitude}",
            variables=["ALLSKY_SFC_SW_DWN", "T2M"],
            payload={},
            error=f"{type(exc).__name__}: {exc}",
            endpoint=endpoint,
        )
