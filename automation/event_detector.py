"""Deterministic event detection over Tools 01-04 and Tool 17 outputs."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class DetectedEvent:
    event_type: str
    severity: str
    reason: str
    priority: int
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _data(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get("data")
    return nested if isinstance(nested, dict) else value


def detect_events(assessment: dict[str, Any], dynamic_pricing: dict[str, Any] | None = None) -> list[DetectedEvent]:
    reserve = _data(assessment.get("reserve"))
    imbalance = _data(assessment.get("imbalance"))
    risk = _data(assessment.get("future_risk"))
    dp = _data(dynamic_pricing or {})
    events: list[DetectedEvent] = []

    reserve_status = str(reserve.get("reserve_status") or "UNKNOWN").upper()
    if reserve_status == "INSUFFICIENT":
        events.append(DetectedEvent(
            "LOW_RESERVE", "HIGH",
            "Available reserve is below the deterministic required reserve for the current horizon.",
            90,
            {"required_reserve_mw": reserve.get("required_reserve_mw"), "actual_reserve_mw": reserve.get("actual_reserve_mw")},
        ))

    imbalance_severity = str(imbalance.get("severity") or "UNKNOWN").upper()
    direction = str(imbalance.get("direction") or "UNKNOWN").upper()
    if imbalance.get("event_required") is True or imbalance_severity in {"HIGH", "CRITICAL"}:
        events.append(DetectedEvent(
            "CURRENT_IMBALANCE", imbalance_severity,
            f"Current {direction.lower()} imbalance crossed the configured operational event threshold.",
            100 if imbalance_severity == "CRITICAL" else 95,
            {"direction": direction, "imbalance_mw": imbalance.get("imbalance_mw"), "severity": imbalance_severity},
        ))
    elif direction == "SURPLUS" and isinstance(imbalance.get("imbalance_mw"), (int, float)) and float(imbalance.get("imbalance_mw")) > 0:
        events.append(DetectedEvent(
            "SOLAR_SURPLUS", imbalance_severity,
            "The current solar-only operating state contains a measurable supply surplus.",
            65,
            {"imbalance_mw": imbalance.get("imbalance_mw"), "severity": imbalance_severity},
        ))

    risk_level = str(risk.get("max_severity") or "UNKNOWN").upper()
    if risk_level == "CRITICAL":
        events.append(DetectedEvent(
            "CRITICAL_FUTURE_RISK", "CRITICAL",
            "Tool 04 detected a critical future-risk interval in the configured planning horizon.",
            110,
            {"risk_event_id": risk.get("risk_event_id"), "highest_risk_period": risk.get("highest_risk_period")},
        ))
    elif risk_level == "HIGH":
        events.append(DetectedEvent(
            "HIGH_FUTURE_RISK", "HIGH",
            "Tool 04 detected a high future-risk interval in the configured planning horizon.",
            92,
            {"risk_event_id": risk.get("risk_event_id"), "highest_risk_period": risk.get("highest_risk_period")},
        ))

    if dp.get("action_required") is True:
        events.append(DetectedEvent(
            "DYNAMIC_PRICING_ACTION_REQUIRED",
            str(dp.get("max_severity") or dp.get("severity") or "MEDIUM").upper(),
            "Tool 17 found a deterministic Dynamic Pricing load-shift action for the current solar-surplus horizon.",
            105,
            {"tool_call_id": (dynamic_pricing or {}).get("tool_call_id"), "total_shifted_mw": dp.get("total_shifted_mw")},
        ))

    # Multiple rules may describe the same underlying state. Keep deterministic
    # priority ordering and unique event types for one monitor cycle.
    unique: dict[str, DetectedEvent] = {}
    for event in events:
        current = unique.get(event.event_type)
        if current is None or event.priority > current.priority:
            unique[event.event_type] = event
    return sorted(unique.values(), key=lambda event: (-event.priority, event.event_type))


def state_signature(snapshot_id: int | None, assessment: dict[str, Any], event: DetectedEvent) -> str:
    """Hash material operating state, excluding ids/timestamps that change every tick."""
    reserve = _data(assessment.get("reserve"))
    imbalance = _data(assessment.get("imbalance"))
    risk = _data(assessment.get("future_risk"))

    def rounded(value: Any):
        return round(float(value), 2) if isinstance(value, (int, float)) else value

    payload = {
        "event_type": event.event_type,
        "severity": event.severity,
        "reserve": {
            "status": reserve.get("reserve_status"),
            "required_mw": rounded(reserve.get("required_reserve_mw")),
            "actual_mw": rounded(reserve.get("actual_reserve_mw")),
            "margin_mw": rounded(reserve.get("reserve_margin_mw")),
        },
        "imbalance": {
            "direction": imbalance.get("direction"),
            "severity": imbalance.get("severity"),
            "imbalance_mw": rounded(imbalance.get("imbalance_mw")),
        },
        "future_risk": {
            "max_severity": risk.get("max_severity"),
        },
        "event_details": {k: rounded(v) for k, v in sorted((event.details or {}).items()) if k not in {"risk_event_id", "tool_call_id"}},
    }
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

