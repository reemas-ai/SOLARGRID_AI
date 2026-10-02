"""Shared external-data quality and freshness helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any, Iterable

QUALITY_STATUSES = {"FRESH", "STALE", "INCOMPLETE", "UNAVAILABLE", "INVALID", "CACHED", "REFERENCE", "NOT_CONFIGURED"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def freshness_status(observed_at: Any, *, max_age_minutes: float, now: datetime | None = None) -> str:
    observed = parse_time(observed_at)
    if observed is None:
        return "INCOMPLETE"
    current = now or utc_now()
    age_minutes = max(0.0, (current - observed).total_seconds() / 60.0)
    return "FRESH" if age_minutes <= float(max_age_minutes) else "STALE"


def numeric_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def required_fields(payload: dict[str, Any] | None, fields: Iterable[str]) -> tuple[bool, list[str]]:
    data = payload or {}
    missing = [field for field in fields if data.get(field) is None]
    return (not missing), missing


def plausible_range(value: Any, minimum: float, maximum: float) -> bool:
    number = numeric_or_none(value)
    return number is not None and minimum <= number <= maximum
