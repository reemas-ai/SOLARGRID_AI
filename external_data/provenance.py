"""Normalized public-data provenance structures."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ExternalPayload:
    source: str
    source_role: str
    status: str
    retrieved_at: datetime
    observed_at: datetime | None = None
    location: str | None = None
    variables: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    endpoint: str | None = None
    cache_origin_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("retrieved_at", "observed_at"):
            value = data.get(key)
            data[key] = value.isoformat() if isinstance(value, datetime) else value
        return data
