"""Shared deterministic helpers for Team 2 tools."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any



def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_dynamic_pricing_context_ref(
    ref, *, expected_snapshot_id: int, expected_horizon_minutes: int,
    db, ToolCallLog, snapshot, interval_minutes: int, consumer_name: str,
):
    """Reload and verify a trusted Tool 17 result by persisted ToolCallLog id.

    This is the shared provenance boundary used by planning, validation and
    execution. Consumers never accept raw Dynamic Pricing engineering values
    from the LLM; they accept only the immutable ToolCallLog reference and
    revalidate the recorded Tool 17 output against the authoritative
    snapshot/horizon.
    """
    if ref is None:
        return None

    from schemas import DynamicPricingResult, DomainStatus, ToolStatus

    log = db.session.get(ToolCallLog, ref.tool_call_id)
    if log is None:
        raise ValueError(f"Unknown Dynamic Pricing ToolCallLog id: {ref.tool_call_id}")
    if str(getattr(log, "tool_name", "")) != "analyze_dynamic_pricing":
        raise ValueError(
            f"ToolCallLog {ref.tool_call_id} is not an analyze_dynamic_pricing result"
        )
    if str(getattr(log, "status", "")).upper() != ToolStatus.SUCCESS.value:
        raise ValueError(
            f"Dynamic Pricing ToolCallLog {ref.tool_call_id} is not SUCCESS"
        )
    if not isinstance(getattr(log, "output_json", None), dict):
        raise ValueError(
            f"Dynamic Pricing ToolCallLog {ref.tool_call_id} has no structured output"
        )

    logged_input = getattr(log, "input_json", None) or {}
    if int(logged_input.get("snapshot_id", -1)) != int(expected_snapshot_id):
        raise ValueError(
            f"Dynamic Pricing ToolCallLog {ref.tool_call_id} input snapshot does not match {consumer_name}"
        )
    if int(logged_input.get("horizon_minutes", -1)) != int(expected_horizon_minutes):
        raise ValueError(
            f"Dynamic Pricing ToolCallLog {ref.tool_call_id} input horizon does not match {consumer_name}"
        )

    result = DynamicPricingResult.model_validate(log.output_json)
    if result.tool_call_id != ref.tool_call_id:
        raise ValueError(
            f"Dynamic Pricing ToolCallLog {ref.tool_call_id} output provenance id does not match the referenced log"
        )
    if int(result.snapshot_id) != int(expected_snapshot_id):
        raise ValueError(
            f"Dynamic Pricing ToolCallLog {ref.tool_call_id} output snapshot does not match {consumer_name}"
        )
    if int(result.horizon_minutes) != int(expected_horizon_minutes):
        raise ValueError(
            f"Dynamic Pricing ToolCallLog {ref.tool_call_id} output horizon does not match {consumer_name}"
        )
    if result.energy_scope != "SOLAR_ONLY":
        raise ValueError("Dynamic Pricing planning context must be SOLAR_ONLY")
    if result.domain_status != DomainStatus.OK:
        raise ValueError(
            f"Dynamic Pricing ToolCallLog {ref.tool_call_id} is not domain-OK: {result.domain_status.value}"
        )

    horizon = int(expected_horizon_minutes)
    interval = int(interval_minutes)
    if interval <= 0 or horizon <= 0 or horizon % interval:
        raise ValueError(f"{consumer_name} horizon must be divisible by interval_minutes")
    count = horizon // interval
    expected_start = utc(snapshot.timestamp)

    seen_indexes = set()
    for adjustment in result.demand_adjustments:
        if adjustment.interval_index >= count:
            raise ValueError(
                f"Dynamic Pricing adjustment interval {adjustment.interval_index} exceeds {consumer_name} horizon"
            )
        expected_time = expected_start + timedelta(
            minutes=(adjustment.interval_index + 1) * interval
        )
        if utc(adjustment.target_time) != expected_time:
            raise ValueError(
                f"Dynamic Pricing adjustment interval {adjustment.interval_index} target_time does not align "
                f"with {consumer_name} dispatch boundary {expected_time.isoformat()}"
            )
        if adjustment.interval_index in seen_indexes:
            raise ValueError("Dynamic Pricing adjustments must be unique per interval")
        seen_indexes.add(adjustment.interval_index)

    for entry in result.load_shift_entries:
        for index, target_time, role in (
            (entry.source_interval_index, entry.source_time, "source"),
            (entry.target_interval_index, entry.target_time, "target"),
        ):
            if index >= count:
                raise ValueError(
                    f"Dynamic Pricing {role} interval {index} exceeds {consumer_name} horizon"
                )
            expected_time = expected_start + timedelta(minutes=(index + 1) * interval)
            if utc(target_time) != expected_time:
                raise ValueError(
                    f"Dynamic Pricing {role} interval {index} time does not align "
                    f"with {consumer_name} dispatch boundary {expected_time.isoformat()}"
                )

    # Execution may impose stronger action-level consistency checks without
    # changing the planning/validation provenance contract.


    return result


def required_reserve_mw(demand_mw: float, solar_values: list[float], policy: dict[str, Any]) -> float:
    formula = policy["required_reserve"]
    changes = [abs(b - a) for a, b in zip(solar_values, solar_values[1:])]
    max_change = max(changes, default=0.0)
    return formula["reserve_fraction"] * demand_mw + formula["solar_variability_fraction"] * max_change


def dispatch_dict(dispatch) -> dict[str, Any]:
    return dispatch.model_dump(mode="json")


def extract_forecast_series(db, Forecast, snapshot, horizon_minutes: int, interval_minutes: int = 15) -> list[dict[str, Any]]:
    """Return aligned solar/demand forecasts without coercing missing values."""
    start = utc(snapshot.timestamp)
    count = horizon_minutes // interval_minutes
    if horizon_minutes % interval_minutes:
        raise ValueError("horizon_minutes must be divisible by interval_minutes")
    rows = Forecast.query.filter(Forecast.target_time >= start,
                                 Forecast.target_time <= start + timedelta(minutes=horizon_minutes)).all()
    by_time: dict[datetime, dict[str, float | None]] = {}
    for row in rows:
        t = utc(row.target_time)
        var = row.variable_name.lower()
        if var not in {"solar", "demand"}:
            continue
        by_time.setdefault(t, {})[var] = row.forecast_value
    result = []
    for i in range(count):
        t = start + timedelta(minutes=i * interval_minutes)
        item = by_time.get(t, {})
        result.append({"target_time": t, "solar": item.get("solar"), "demand": item.get("demand")})
    return result


def ensure_known_series(series: list[dict[str, Any]]) -> None:
    for i, point in enumerate(series):
        if point.get("solar") is None or point.get("demand") is None:
            raise ValueError(f"UNKNOWN forecast data at interval {i}; planning cannot safely proceed")


def estimate_plan_reserve(dispatch, generators, batteries, interval_minutes: int) -> float | None:
    """Deterministic first-interval upward headroom estimate.

    This is a physical headroom measure, not a replacement for Tool 02's
    authoritative reserve assessment. It is used only as a plan metric.
    """
    if not dispatch.intervals:
        return None
    first = dispatch.intervals[0]
    total = 0.0
    for g in generators:
        p = first.generators.get(str(g.id))
        if p is None or p.output_mw is None or g.max_output_mw is None:
            return None
        total += max(0.0, g.max_output_mw - p.output_mw)
    dt_h = interval_minutes / 60.0
    for b in batteries:
        p = first.batteries.get(str(b.id))
        if p is None or p.power_mw is None or b.max_discharge_mw is None or b.soc_pct is None or b.capacity_mwh is None:
            return None
        energy_above_min = b.capacity_mwh * (b.soc_pct - (b.min_soc_pct or 0.0)) / 100.0
        eff = b.efficiency
        if eff is None or eff <= 0:
            return None
        energy_limited_discharge = energy_above_min * eff / dt_h if dt_h > 0 else 0.0
        total += max(0.0, min(b.max_discharge_mw, energy_limited_discharge) - max(0.0, p.power_mw))
    return total


def configured_generator_costs() -> dict[str, float]:
    from config_loader import load_generator_seed
    return {str(x["id"]): float(x["fuel_cost_usd_per_mwh"]) for x in load_generator_seed().get("units", []) if x.get("fuel_cost_usd_per_mwh") is not None}


def plan_dispatch_from_record(plan, PlanDispatch):
    payload = plan.actions
    if isinstance(payload, dict) and "intervals" in payload:
        return PlanDispatch.model_validate(payload)
    raise ValueError("Plan does not contain the canonical PlanDispatch in actions")
