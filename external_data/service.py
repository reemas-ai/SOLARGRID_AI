"""External-source orchestration, persistence, cache fallback and provenance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from config_loader import load_json
from database import ExternalDataProvenance, db

from .eia_adapter import fetch_benchmark
from .nasa_power_adapter import fetch_hourly_reference
from .open_meteo_adapter import fetch_forecast
from .provenance import ExternalPayload


class ExternalDataService:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or load_json("external_data_v1.json")

    def refresh(self, source: str, *, consumed_by: str | None = None, run_id: int | None = None) -> ExternalDataProvenance:
        source = str(source).upper()
        if source == "OPEN_METEO":
            cfg = self.config.get("open_meteo") or {}
            location = self.config.get("location") or {}
            payload = fetch_forecast(
                place_name=location.get("place_name"),
                latitude=location.get("latitude"),
                longitude=location.get("longitude"),
                forecast_hours=int(cfg.get("forecast_hours", 6)),
                freshness_minutes=int((self.config.get("freshness") or {}).get("weather_minutes", 30)),
                timeout_seconds=float(cfg.get("timeout_seconds", 10)),
            )
        elif source == "NASA_POWER":
            cfg = self.config.get("nasa_power") or {}
            location = self.config.get("location") or {}
            lat = location.get("latitude")
            lon = location.get("longitude")
            if lat is None or lon is None:
                # Reuse latest Open-Meteo coordinates without making up a location.
                latest = self.latest_usable("OPEN_METEO")
                data = (latest.payload_json or {}) if latest else {}
                lat, lon = data.get("latitude"), data.get("longitude")
            if lat is None or lon is None:
                payload = ExternalPayload(
                    source="NASA_POWER", source_role="HISTORICAL_SOLAR_REFERENCE", status="NOT_CONFIGURED",
                    retrieved_at=datetime.now(timezone.utc), variables=["ALLSKY_SFC_SW_DWN", "T2M"], payload={},
                    error="Latitude/longitude unavailable; refresh Open-Meteo or configure coordinates first.",
                )
            else:
                payload = fetch_hourly_reference(
                    latitude=float(lat), longitude=float(lon),
                    lookback_days=int(cfg.get("lookback_days", 7)),
                    timeout_seconds=float(cfg.get("timeout_seconds", 15)),
                )
        elif source == "US_EIA":
            payload = fetch_benchmark(config=self.config.get("eia") or {})
        else:
            raise ValueError(f"Unsupported external source: {source}")

        record = self._persist_with_fallback(payload, consumed_by=consumed_by, run_id=run_id)
        return record

    def refresh_all(self, *, consumed_by: str | None = None, run_id: int | None = None) -> list[ExternalDataProvenance]:
        rows: list[ExternalDataProvenance] = []
        for source, key in (("OPEN_METEO", "open_meteo"), ("NASA_POWER", "nasa_power"), ("US_EIA", "eia")):
            cfg = self.config.get(key) or {}
            if cfg.get("enabled", True) is False:
                continue
            rows.append(self.refresh(source, consumed_by=consumed_by, run_id=run_id))
        return rows

    def latest(self, source: str) -> ExternalDataProvenance | None:
        return (
            ExternalDataProvenance.query.filter_by(source=str(source).upper())
            .order_by(ExternalDataProvenance.retrieved_at.desc(), ExternalDataProvenance.id.desc())
            .first()
        )

    def latest_usable(self, source: str) -> ExternalDataProvenance | None:
        return (
            ExternalDataProvenance.query
            .filter(ExternalDataProvenance.source == str(source).upper())
            .filter(ExternalDataProvenance.status.in_(["FRESH", "CACHED", "REFERENCE"]))
            .order_by(ExternalDataProvenance.retrieved_at.desc(), ExternalDataProvenance.id.desc())
            .first()
        )

    def status(self) -> list[dict[str, Any]]:
        rows = []
        for source in ("OPEN_METEO", "NASA_POWER", "US_EIA"):
            item = self.latest(source)
            if item is None:
                rows.append({"source": source, "status": "NOT_CHECKED"})
            else:
                rows.append(self.serialize(item))
        return rows

    def mark_consumed(self, record_id: int, *, consumed_by: str, run_id: int | None = None, snapshot_id: int | None = None) -> None:
        record = db.session.get(ExternalDataProvenance, int(record_id))
        if record is None:
            return
        record.consumed_by = consumed_by
        if run_id is not None:
            record.consumed_run_id = run_id
        if snapshot_id is not None:
            record.consumed_snapshot_id = snapshot_id
        db.session.commit()

    @staticmethod
    def serialize(row: ExternalDataProvenance) -> dict[str, Any]:
        return {
            "id": row.id,
            "source": row.source,
            "source_role": row.source_role,
            "status": row.status,
            "retrieved_at": row.retrieved_at.isoformat() if row.retrieved_at else None,
            "observed_at": row.observed_at.isoformat() if row.observed_at else None,
            "location": row.location,
            "variables": row.variables_json or [],
            "payload": row.payload_json or {},
            "error": row.error_message,
            "endpoint": row.endpoint,
            "cache_origin_id": row.cache_origin_id,
            "consumed_by": row.consumed_by,
            "consumed_run_id": row.consumed_run_id,
            "consumed_snapshot_id": row.consumed_snapshot_id,
        }

    def _persist_with_fallback(self, payload: ExternalPayload, *, consumed_by: str | None, run_id: int | None) -> ExternalDataProvenance:
        fallback = None
        if payload.status in {"UNAVAILABLE", "STALE", "INCOMPLETE", "INVALID"}:
            fallback = self.latest_usable(payload.source)
            if fallback is not None:
                freshness_cfg = self.config.get("freshness") or {}
                max_cache = int(freshness_cfg.get("cache_max_minutes", 120))
                origin = fallback
                if fallback.cache_origin_id is not None:
                    origin = db.session.get(ExternalDataProvenance, int(fallback.cache_origin_id)) or fallback
                origin_time = origin.retrieved_at or fallback.retrieved_at
                age = datetime.now(timezone.utc).replace(tzinfo=None) - origin_time
                if age <= timedelta(minutes=max_cache):
                    payload = ExternalPayload(
                        source=payload.source,
                        source_role=payload.source_role,
                        status="CACHED",
                        retrieved_at=datetime.now(timezone.utc),
                        observed_at=origin.observed_at.replace(tzinfo=timezone.utc) if origin.observed_at else None,
                        location=origin.location,
                        variables=list(origin.variables_json or []),
                        payload=dict(origin.payload_json or {}),
                        error=payload.error or f"Live source status was {payload.status}; using bounded last-known-good cache.",
                        endpoint=payload.endpoint,
                        cache_origin_id=origin.id,
                    )

        record = ExternalDataProvenance(
            source=payload.source,
            source_role=payload.source_role,
            status=payload.status,
            retrieved_at=payload.retrieved_at.replace(tzinfo=None),
            observed_at=payload.observed_at.replace(tzinfo=None) if payload.observed_at else None,
            location=payload.location,
            variables_json=payload.variables,
            payload_json=payload.payload,
            error_message=payload.error,
            endpoint=payload.endpoint,
            cache_origin_id=payload.cache_origin_id,
            consumed_by=consumed_by,
            consumed_run_id=run_id,
        )
        db.session.add(record)
        db.session.commit()
        return record
