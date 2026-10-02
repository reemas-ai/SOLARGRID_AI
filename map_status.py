"""Three-state map status contract for SolarGrid UI markers.

The web map intentionally exposes only three operator-facing states:
GREEN (Healthy), YELLOW (Attention), RED (Critical).
No gray/unknown marker is allowed. If a backend availability value is missing or
unrecognized, the map degrades safely to YELLOW so the operator sees that the
asset needs review instead of a fourth ambiguous color.
"""
from __future__ import annotations

VALID_MAP_STATUSES = frozenset({"GREEN", "YELLOW", "RED"})

_GREEN_AVAILABILITY = frozenset({"AVAILABLE", "ONLINE", "NORMAL", "HEALTHY"})
_YELLOW_AVAILABILITY = frozenset({"DEGRADED", "WARNING", "LIMITED", "ATTENTION"})
_RED_AVAILABILITY = frozenset({"UNAVAILABLE", "TRIPPED", "FAULT", "OFFLINE", "CRITICAL"})


def status_from_availability(availability: object) -> tuple[str, list[str]]:
    """Map an engineering availability token to the three-state UI contract."""
    raw = str(availability or "").strip().upper()
    if raw in _GREEN_AVAILABILITY:
        return "GREEN", []
    if raw in _YELLOW_AVAILABILITY:
        return "YELLOW", [f"Availability: {raw}"]
    if raw in _RED_AVAILABILITY:
        return "RED", [f"Availability: {raw}"]

    label = raw or "MISSING"
    return "YELLOW", [f"Availability requires operator review: {label}"]


def normalize_map_status(status: object) -> str:
    """Return a valid tri-color status, using YELLOW as the safe fallback."""
    value = str(status or "").strip().upper()
    return value if value in VALID_MAP_STATUSES else "YELLOW"


def reference_site_status() -> tuple[str, list[str]]:
    """Map-only references are healthy in the demo unless a demo fault is assigned.

    This is explicitly a UI demo state, not real utility telemetry. Reference rows
    never enter planning, reserve, execution, safety, or power-flow calculations.
    """
    return "GREEN", ["Demo map status: healthy; reference project is not live telemetry"]
