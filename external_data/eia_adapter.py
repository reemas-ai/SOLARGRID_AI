"""U.S. EIA Open Data benchmark adapter.

EIA data is an external electricity-system benchmark/reference, never Jordan
telemetry.  The adapter is intentionally configuration-driven because EIA v2
routes/facets identify different benchmark series.
"""
from __future__ import annotations

import os
from typing import Any

import requests

from .provenance import ExternalPayload
from .quality import utc_now


def fetch_benchmark(*, config: dict[str, Any], timeout_seconds: float = 12.0) -> ExternalPayload:
    retrieved = utc_now()
    key = (os.getenv(str(config.get("api_key_env") or "EIA_API_KEY")) or "").strip()
    route = str(config.get("route") or "").strip().lstrip("/")
    base = (os.getenv("EIA_BASE_URL") or "https://api.eia.gov/v2/").rstrip("/") + "/"
    endpoint = base + route if route else base
    if not key or not route:
        missing = []
        if not key:
            missing.append("API key")
        if not route:
            missing.append("benchmark route")
        return ExternalPayload(
            source="US_EIA",
            source_role="EXTERNAL_ELECTRICITY_BENCHMARK",
            status="NOT_CONFIGURED",
            retrieved_at=retrieved,
            variables=list(config.get("variables") or []),
            payload={},
            error="Missing " + " and ".join(missing),
            endpoint=endpoint,
        )
    try:
        params: list[tuple[str, str]] = [("api_key", key)]
        if config.get("frequency"):
            params.append(("frequency", str(config["frequency"])))
        for variable in config.get("variables") or []:
            params.append(("data[]", str(variable)))
        for facet_name, values in (config.get("facets") or {}).items():
            for value in values or []:
                params.append((f"facets[{facet_name}][]", str(value)))
        params.extend([("offset", "0"), ("length", str(int(config.get("length", 24))))])
        response = requests.get(endpoint, params=params, timeout=timeout_seconds)
        response.raise_for_status()
        raw = response.json()
        data = ((raw.get("response") or {}).get("data")) or []
        if not isinstance(data, list) or not data:
            raise ValueError("EIA benchmark returned no data rows")
        # API key is never persisted because only the response payload is stored.
        return ExternalPayload(
            source="US_EIA",
            source_role="EXTERNAL_ELECTRICITY_BENCHMARK",
            status="REFERENCE",
            retrieved_at=retrieved,
            variables=list(config.get("variables") or []),
            payload={"rows": data[: int(config.get("length", 24))], "benchmark_only": True},
            endpoint=endpoint,
        )
    except Exception as exc:
        return ExternalPayload(
            source="US_EIA",
            source_role="EXTERNAL_ELECTRICITY_BENCHMARK",
            status="UNAVAILABLE",
            retrieved_at=retrieved,
            variables=list(config.get("variables") or []),
            payload={},
            error=f"{type(exc).__name__}: {exc}",
            endpoint=endpoint,
        )
