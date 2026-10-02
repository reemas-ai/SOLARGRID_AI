"""SolarGrid AI — Dynamic Pricing deterministic core (Tool 17, Phase 2).

This module contains the SolarGrid-native, solar-only deterministic logic for
Dynamic Pricing.  It deliberately does not register Tool 17, read the database,
call an LLM, create Plans, execute actions, or bypass the existing validation
lifecycle.  Phase 3 will add the authoritative state/forecast adapter and tool
registration only after this core remains regression-safe.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from config_loader import load_dynamic_pricing_config
from runtime import finish_log, tool_call
from tool_common import utc
from schemas import (
    DomainStatus,
    ToolStatus,
    DynamicPricingDemandAdjustment,
    DynamicPricingExpectedImpact,
    DynamicPricingHourAnalysis,
    DynamicPricingLoadShiftEntry,
    DynamicPricingOutcomeMetrics,
    DynamicPricingRequest,
    DynamicPricingResult,
    DynamicPricingSeverity,
)


@dataclass(frozen=True)
class DynamicPricingIntervalContext:
    """Internal deterministic computation context for one planning interval.

    This is intentionally *not* a second public schema system.  It is the
    internal adapter shape that Phase 3 will populate from authoritative
    SolarGrid snapshot/forecast/assets data before invoking this core.
    """

    interval_index: int
    target_time: datetime
    solar_generation_mw: float | None
    demand_mw: float | None
    other_generation_mw: float | None
    battery_charging_headroom_mw: float | None
    flexible_load_mw: float | None
    max_shiftable_load_mw: float | None
    export_capacity_mw: float | None = None
    baseline_price_per_mwh: float | None = None


@dataclass(frozen=True)
class _SurplusDetection:
    interval_index: int
    target_time: datetime
    surplus_exists: bool
    surplus_mw: float
    solar_generation_mw: float
    demand_mw: float
    absorbable_mw: float
    curtailment_risk: bool


@dataclass(frozen=True)
class _SurplusSeverity:
    interval_index: int
    target_time: datetime
    severity: DynamicPricingSeverity
    surplus_mw: float
    surplus_ratio: float


@dataclass(frozen=True)
class _PricingSignal:
    interval_index: int
    target_time: datetime
    baseline_price_per_mwh: float
    discount_ratio: float
    final_price_per_mwh: float


@dataclass(frozen=True)
class _LoadResponse:
    interval_index: int
    target_time: datetime
    expected_shifted_load_mw: float
    remaining_flexible_load_mw: float
    response_ratio: float


@dataclass(frozen=True)
class _LoadShiftPlan:
    entries: tuple[DynamicPricingLoadShiftEntry, ...]
    total_shifted_mw: float
    target_interval_indexes: tuple[int, ...]


def _pricing(config: dict[str, Any]) -> dict[str, Any]:
    return config["pricing"]


def _surplus_config(config: dict[str, Any]) -> dict[str, Any]:
    return config["surplus_detection"]


def _flex_config(config: dict[str, Any]) -> dict[str, Any]:
    return config["flexible_load"]


def _require_nonnegative(value: float, field_name: str) -> float:
    number = float(value)
    if number < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return number


def _known_context_error(intervals: Iterable[DynamicPricingIntervalContext]) -> str | None:
    required_fields = (
        "solar_generation_mw",
        "demand_mw",
        "other_generation_mw",
        "battery_charging_headroom_mw",
        "flexible_load_mw",
        "max_shiftable_load_mw",
    )
    for interval in intervals:
        for field_name in required_fields:
            if getattr(interval, field_name) is None:
                return (
                    f"Required Dynamic Pricing input '{field_name}' is unavailable "
                    f"for interval {interval.interval_index}"
                )
    return None


def _validate_interval_identity(intervals: list[DynamicPricingIntervalContext]) -> None:
    indexes = [item.interval_index for item in intervals]
    if len(indexes) != len(set(indexes)):
        raise ValueError("Dynamic Pricing interval indexes must be unique")
    times = [item.target_time for item in intervals]
    if len(times) != len(set(times)):
        raise ValueError("Dynamic Pricing target times must be unique")
    if any(index < 0 for index in indexes):
        raise ValueError("Dynamic Pricing interval indexes must be non-negative")


def detect_solar_surplus(
    interval: DynamicPricingIntervalContext,
    config: dict[str, Any],
) -> _SurplusDetection:
    """Detect solar surplus using the validated standalone absorption semantics.

    Solar surplus is not reduced to ``solar - demand``.  It accounts for
    demand headroom remaining after other/must-run generation plus available
    battery charging and export absorption capacity.
    """

    if interval.solar_generation_mw is None:
        raise ValueError("solar_generation_mw is required")
    if interval.demand_mw is None:
        raise ValueError("demand_mw is required")
    if interval.other_generation_mw is None:
        raise ValueError("other_generation_mw is required")
    if interval.battery_charging_headroom_mw is None:
        raise ValueError("battery_charging_headroom_mw is required")

    solar_mw = _require_nonnegative(interval.solar_generation_mw, "solar_generation_mw")
    demand_mw = _require_nonnegative(interval.demand_mw, "demand_mw")
    other_generation_mw = _require_nonnegative(interval.other_generation_mw, "other_generation_mw")
    battery_headroom_mw = _require_nonnegative(
        interval.battery_charging_headroom_mw,
        "battery_charging_headroom_mw",
    )

    configured_export = float(_surplus_config(config)["export_interconnection_capacity_mw"])
    export_capacity_mw = (
        configured_export
        if interval.export_capacity_mw is None
        else _require_nonnegative(interval.export_capacity_mw, "export_capacity_mw")
    )

    absorbable_mw = battery_headroom_mw + export_capacity_mw
    net_demand_headroom = max(demand_mw - other_generation_mw, 0.0)
    total_absorption_capacity = net_demand_headroom + absorbable_mw
    surplus_mw = max(solar_mw - total_absorption_capacity, 0.0)

    threshold = float(_surplus_config(config)["surplus_threshold_mw"])
    surplus_exists = surplus_mw > threshold
    curtailment_risk = surplus_mw > absorbable_mw

    return _SurplusDetection(
        interval_index=interval.interval_index,
        target_time=interval.target_time,
        surplus_exists=surplus_exists,
        surplus_mw=round(surplus_mw, 3),
        solar_generation_mw=round(solar_mw, 3),
        demand_mw=round(demand_mw, 3),
        absorbable_mw=round(absorbable_mw, 3),
        curtailment_risk=curtailment_risk,
    )


def classify_surplus_severity(
    detection: _SurplusDetection,
    config: dict[str, Any],
) -> _SurplusSeverity:
    """Classify the deterministic solar-surplus amount into configured bands."""

    thresholds = _surplus_config(config)["severity_thresholds_mw"]
    low = float(thresholds["low"])
    medium = float(thresholds["medium"])
    high = float(thresholds["high"])
    critical = float(thresholds["critical"])

    surplus_mw = detection.surplus_mw
    ratio = surplus_mw / detection.demand_mw if detection.demand_mw > 0 else 0.0

    if not detection.surplus_exists or surplus_mw <= 0:
        severity = DynamicPricingSeverity.NONE
    elif surplus_mw < low:
        severity = DynamicPricingSeverity.LOW
    elif surplus_mw < medium:
        severity = DynamicPricingSeverity.LOW
    elif surplus_mw < high:
        severity = DynamicPricingSeverity.MEDIUM
    elif surplus_mw < critical:
        severity = DynamicPricingSeverity.HIGH
    else:
        severity = DynamicPricingSeverity.CRITICAL

    return _SurplusSeverity(
        interval_index=detection.interval_index,
        target_time=detection.target_time,
        severity=severity,
        surplus_mw=surplus_mw,
        surplus_ratio=round(ratio, 4),
    )


def calculate_dynamic_price(
    severity: _SurplusSeverity,
    baseline_price_per_mwh: float,
    config: dict[str, Any],
) -> _PricingSignal:
    """Calculate the deterministic price signal with cap/floor invariants."""

    pricing_cfg = _pricing(config)
    baseline = _require_nonnegative(baseline_price_per_mwh, "baseline_price_per_mwh")
    max_discount = float(pricing_cfg["max_discount_ratio"])
    price_floor = float(pricing_cfg["min_price_per_mwh"])
    critical = float(_surplus_config(config)["severity_thresholds_mw"]["critical"])

    if severity.severity == DynamicPricingSeverity.NONE:
        normalized_index = 0.0
    elif severity.surplus_mw >= critical:
        normalized_index = 1.0
    else:
        normalized_index = min(severity.surplus_mw / critical, 1.0)

    requested_discount = max(0.0, min(normalized_index * max_discount, max_discount))
    raw_price = baseline * (1.0 - requested_discount)
    final_price = max(raw_price, price_floor)

    if baseline > 0:
        effective_discount = max(0.0, 1.0 - final_price / baseline)
    else:
        effective_discount = 0.0

    return _PricingSignal(
        interval_index=severity.interval_index,
        target_time=severity.target_time,
        baseline_price_per_mwh=round(baseline, 2),
        discount_ratio=round(effective_discount, 4),
        final_price_per_mwh=round(final_price, 2),
    )


def simulate_flexible_load_response(
    interval: DynamicPricingIntervalContext,
    pricing_signal: _PricingSignal,
    config: dict[str, Any],
) -> _LoadResponse:
    """Calculate bounded flexible-load response to the deterministic discount."""

    if interval.flexible_load_mw is None:
        raise ValueError("flexible_load_mw is required")
    if interval.max_shiftable_load_mw is None:
        raise ValueError("max_shiftable_load_mw is required")

    available = _require_nonnegative(interval.flexible_load_mw, "flexible_load_mw")
    max_shiftable = _require_nonnegative(interval.max_shiftable_load_mw, "max_shiftable_load_mw")
    shiftable_ceiling = min(available, max_shiftable)

    sensitivity = float(_flex_config(config)["price_sensitivity"])
    response_ratio = max(0.0, min(pricing_signal.discount_ratio * sensitivity, 1.0))
    expected_shift = min(shiftable_ceiling * response_ratio, shiftable_ceiling)
    remaining = max(available - expected_shift, 0.0)

    return _LoadResponse(
        interval_index=interval.interval_index,
        target_time=interval.target_time,
        expected_shifted_load_mw=round(expected_shift, 3),
        remaining_flexible_load_mw=round(remaining, 3),
        response_ratio=round(response_ratio, 4),
    )


def _target_interval_limit(
    intervals: list[DynamicPricingIntervalContext],
    max_target_hours: int,
) -> int:
    """Convert the standalone hour-based target limit to the current interval grid."""

    if max_target_hours <= 0:
        return 0
    if len(intervals) < 2:
        return 1

    ordered_times = sorted(item.target_time for item in intervals)
    deltas_minutes = [
        (b - a).total_seconds() / 60.0
        for a, b in zip(ordered_times, ordered_times[1:])
    ]
    if any(delta <= 0 for delta in deltas_minutes):
        raise ValueError("Dynamic Pricing interval duration must be positive")

    interval_minutes = deltas_minutes[0]
    tolerance = 1e-6
    if any(abs(delta - interval_minutes) > tolerance for delta in deltas_minutes[1:]):
        raise ValueError("Dynamic Pricing intervals must use a uniform duration")

    return max(1, int((max_target_hours * 60.0) // interval_minutes))


def schedule_load_shift(
    intervals: list[DynamicPricingIntervalContext],
    detections: dict[int, _SurplusDetection],
    responses: dict[int, _LoadResponse],
    config: dict[str, Any],
) -> _LoadShiftPlan:
    """Build a source-to-target load shift while conserving total demand."""

    flex_cfg = _flex_config(config)
    max_target_hours = int(flex_cfg["max_target_hours"])
    max_target_intervals = _target_interval_limit(intervals, max_target_hours)
    max_system_shift = float(flex_cfg["max_flexible_shift_mw"])

    targets = sorted(
        [
            item
            for item in intervals
            if detections[item.interval_index].surplus_exists
            and responses[item.interval_index].expected_shifted_load_mw > 0
        ],
        key=lambda item: detections[item.interval_index].surplus_mw,
        reverse=True,
    )[:max_target_intervals]

    sources = sorted(
        [
            item
            for item in intervals
            if not detections[item.interval_index].surplus_exists
            and (item.flexible_load_mw or 0.0) > 0
        ],
        key=lambda item: float(item.flexible_load_mw or 0.0),
        reverse=True,
    )

    target_remaining = {
        item.interval_index: responses[item.interval_index].expected_shifted_load_mw
        for item in targets
    }
    source_remaining = {
        item.interval_index: float(item.flexible_load_mw or 0.0)
        for item in sources
    }

    remaining_system_shift = max_system_shift
    entries: list[DynamicPricingLoadShiftEntry] = []
    total_shifted = 0.0

    for target in targets:
        if target_remaining[target.interval_index] <= 0 or remaining_system_shift <= 0:
            continue
        for source in sources:
            if target_remaining[target.interval_index] <= 0 or remaining_system_shift <= 0:
                break
            if source_remaining[source.interval_index] <= 0:
                continue

            move_mw = min(
                target_remaining[target.interval_index],
                source_remaining[source.interval_index],
                remaining_system_shift,
            )
            if move_mw <= 0:
                continue

            rounded_move = round(move_mw, 3)
            entries.append(
                DynamicPricingLoadShiftEntry(
                    source_interval_index=source.interval_index,
                    source_time=source.target_time,
                    target_interval_index=target.interval_index,
                    target_time=target.target_time,
                    shifted_mw=rounded_move,
                )
            )
            target_remaining[target.interval_index] -= move_mw
            source_remaining[source.interval_index] -= move_mw
            remaining_system_shift -= move_mw
            total_shifted += rounded_move

    return _LoadShiftPlan(
        entries=tuple(entries),
        total_shifted_mw=round(total_shifted, 3),
        target_interval_indexes=tuple(item.interval_index for item in targets),
    )


def _aggregate_demand_adjustments(
    intervals: list[DynamicPricingIntervalContext],
    entries: Iterable[DynamicPricingLoadShiftEntry],
) -> list[DynamicPricingDemandAdjustment]:
    by_index = {item.interval_index: item for item in intervals}
    deltas: dict[int, float] = {}
    for entry in entries:
        deltas[entry.source_interval_index] = deltas.get(entry.source_interval_index, 0.0) - entry.shifted_mw
        deltas[entry.target_interval_index] = deltas.get(entry.target_interval_index, 0.0) + entry.shifted_mw

    return [
        DynamicPricingDemandAdjustment(
            interval_index=index,
            target_time=by_index[index].target_time,
            demand_delta_mw=round(delta, 3),
        )
        for index, delta in sorted(deltas.items())
        if abs(delta) > 1e-9
    ]



def _impact_point(*, solar_mw: float, demand_mw: float, other_generation_mw: float,
                  battery_headroom_mw: float, export_capacity_mw: float) -> dict[str, float]:
    """Return the standalone-equivalent solar-surplus impact metrics for one interval."""
    solar = max(float(solar_mw), 0.0)
    demand = max(float(demand_mw), 0.0)
    other = max(float(other_generation_mw), 0.0)
    battery = max(float(battery_headroom_mw), 0.0)
    export = max(float(export_capacity_mw), 0.0)
    absorbable = battery + export
    net_demand_headroom = max(demand - other, 0.0)
    total_absorption_capacity = net_demand_headroom + absorbable
    surplus = max(solar - total_absorption_capacity, 0.0)
    curtailment = max(surplus - absorbable, 0.0)
    used_solar = max(solar - surplus, 0.0)
    utilization = (used_solar / solar) if solar > 0 else 1.0
    return {
        "surplus_mw": surplus,
        "curtailment_risk_mw": curtailment,
        "utilization_ratio": utilization,
    }


def _impact_reduction_from_rows(before_rows: list[dict[str, float]], after_rows: list[dict[str, float]]) -> dict[str, float]:
    before_surplus = sum(row["surplus_mw"] for row in before_rows)
    after_surplus = sum(row["surplus_mw"] for row in after_rows)
    before_curtailment = sum(row["curtailment_risk_mw"] for row in before_rows)
    after_curtailment = sum(row["curtailment_risk_mw"] for row in after_rows)
    before_util = sum(row["utilization_ratio"] for row in before_rows) / len(before_rows) if before_rows else 0.0
    after_util = sum(row["utilization_ratio"] for row in after_rows) / len(after_rows) if after_rows else 0.0
    return {
        "surplus_reduction_mw": round(max(before_surplus - after_surplus, 0.0), 3),
        "curtailment_reduction_mw": round(max(before_curtailment - after_curtailment, 0.0), 3),
        "utilization_improvement_ratio": round(max(after_util - before_util, 0.0), 4),
    }


def calculate_expected_dynamic_pricing_impact(
    intervals: Iterable[DynamicPricingIntervalContext],
    adjustments: Iterable[DynamicPricingDemandAdjustment],
    config: dict[str, Any],
) -> DynamicPricingExpectedImpact:
    """Compute Tool 17 expected impact using the validated standalone semantics."""
    resolved = sorted(list(intervals), key=lambda item: item.interval_index)
    delta_by_index = {int(item.interval_index): float(item.demand_delta_mw) for item in adjustments}
    before_rows: list[dict[str, float]] = []
    after_rows: list[dict[str, float]] = []
    shifted_in = 0.0
    configured_export = float(_surplus_config(config)["export_interconnection_capacity_mw"])
    for interval in resolved:
        values = (
            interval.solar_generation_mw,
            interval.demand_mw,
            interval.other_generation_mw,
            interval.battery_charging_headroom_mw,
        )
        if any(value is None for value in values):
            return DynamicPricingExpectedImpact()
        delta = delta_by_index.get(interval.interval_index, 0.0)
        shifted_in += max(delta, 0.0)
        export_capacity = configured_export if interval.export_capacity_mw is None else float(interval.export_capacity_mw)
        before_rows.append(_impact_point(
            solar_mw=float(interval.solar_generation_mw),
            demand_mw=float(interval.demand_mw),
            other_generation_mw=float(interval.other_generation_mw),
            battery_headroom_mw=float(interval.battery_charging_headroom_mw),
            export_capacity_mw=export_capacity,
        ))
        after_rows.append(_impact_point(
            solar_mw=float(interval.solar_generation_mw),
            demand_mw=max(float(interval.demand_mw) + delta, 0.0),
            other_generation_mw=float(interval.other_generation_mw),
            battery_headroom_mw=float(interval.battery_charging_headroom_mw),
            export_capacity_mw=export_capacity,
        ))
    reductions = _impact_reduction_from_rows(before_rows, after_rows)
    return DynamicPricingExpectedImpact(
        expected_load_shift_mw=round(shifted_in, 3),
        expected_surplus_reduction_mw=reductions["surplus_reduction_mw"],
        expected_curtailment_reduction_mw=reductions["curtailment_reduction_mw"],
        expected_utilization_improvement_ratio=reductions["utilization_improvement_ratio"],
    )


def _relative_deviation(expected: float | None, actual: float | None) -> float | None:
    if expected is None or actual is None:
        return None
    expected = float(expected)
    actual = float(actual)
    if abs(expected) <= 1e-12:
        return 0.0 if abs(actual) <= 1e-12 else 1.0
    return round((actual - expected) / expected, 4)


def evaluate_dynamic_pricing_outcome(
    result: DynamicPricingResult,
    actual_actions: dict[str, Any] | None,
    *,
    deviation_tolerance_ratio: float,
) -> DynamicPricingOutcomeMetrics:
    """Compare Tool 17 expected impact with persisted Tool 13 actual actions.

    Load-shift performance is always evaluated when the execution persisted a
    Dynamic Pricing actual-action block.  Surplus/curtailment/utilization impact
    is evaluated only when the actual interval records contain both pre/post-DP
    demand and executed solar output. Missing evidence stays None.
    """
    actual_actions = actual_actions if isinstance(actual_actions, dict) else {}
    dp_actual = actual_actions.get("dynamic_pricing") if isinstance(actual_actions.get("dynamic_pricing"), dict) else None
    expected = result.expected_impact
    if dp_actual is None:
        return DynamicPricingOutcomeMetrics(
            tool_call_id=result.tool_call_id,
            verification_status="NOT_EVALUATED",
            goal_result="NOT_EVALUATED",
            expected_load_shift_mw=expected.expected_load_shift_mw,
            expected_surplus_reduction_mw=expected.expected_surplus_reduction_mw,
            expected_curtailment_reduction_mw=expected.expected_curtailment_reduction_mw,
            expected_utilization_improvement_ratio=expected.expected_utilization_improvement_ratio,
            deviations={},
            deviation_tolerance_ratio=deviation_tolerance_ratio,
            significant_deviation=None,
            evidence_basis=["Tool 13 actual_actions.dynamic_pricing is unavailable"],
        )

    actual_shift = dp_actual.get("total_shifted_mw")
    actual_shift = float(actual_shift) if actual_shift is not None else None
    analyses = {int(item.interval_index): item for item in result.hourly_analysis}
    before_rows: list[dict[str, float]] = []
    after_rows: list[dict[str, float]] = []
    complete_impact_evidence = bool(analyses)
    interval_rows = actual_actions.get("intervals") if isinstance(actual_actions.get("intervals"), list) else []
    actual_by_index = {
        int(row.get("index")): row
        for row in interval_rows
        if isinstance(row, dict) and row.get("index") is not None
    }
    for index, analysis in sorted(analyses.items()):
        row = actual_by_index.get(index)
        if not isinstance(row, dict):
            complete_impact_evidence = False
            break
        before_demand = row.get("demand_before_dynamic_pricing_mw")
        after_demand = row.get("demand_after_dynamic_pricing_mw")
        generator_rows = row.get("generators") if isinstance(row.get("generators"), list) else []
        actual_solar_values = [
            item.get("actual_mw") for item in generator_rows
            if isinstance(item, dict) and item.get("actual_mw") is not None
        ]
        required = (
            before_demand,
            after_demand,
            analysis.other_generation_mw,
            analysis.battery_charging_headroom_mw,
            analysis.export_capacity_mw,
        )
        if not actual_solar_values or any(value is None for value in required):
            complete_impact_evidence = False
            break
        actual_solar = sum(float(value) for value in actual_solar_values)
        common = dict(
            solar_mw=actual_solar,
            other_generation_mw=float(analysis.other_generation_mw),
            battery_headroom_mw=float(analysis.battery_charging_headroom_mw),
            export_capacity_mw=float(analysis.export_capacity_mw),
        )
        before_rows.append(_impact_point(demand_mw=float(before_demand), **common))
        after_rows.append(_impact_point(demand_mw=float(after_demand), **common))

    actual_surplus = actual_curtailment = actual_utilization = None
    evidence_basis = ["Tool 17 expected_impact", "Tool 13 persisted Dynamic Pricing actual load shift"]
    if complete_impact_evidence and before_rows and len(before_rows) == len(analyses):
        reductions = _impact_reduction_from_rows(before_rows, after_rows)
        actual_surplus = reductions["surplus_reduction_mw"]
        actual_curtailment = reductions["curtailment_reduction_mw"]
        actual_utilization = reductions["utilization_improvement_ratio"]
        evidence_basis.append("Tool 13 per-interval executed solar and demand before/after Dynamic Pricing")
        evidence_basis.append("Tool 17 battery/export/other-generation interval assumptions")
    else:
        evidence_basis.append("Full impact metrics remain UNKNOWN because interval actual solar/demand evidence is incomplete")

    deviations = {
        "load_shift_mw": _relative_deviation(expected.expected_load_shift_mw, actual_shift),
        "surplus_reduction_mw": _relative_deviation(expected.expected_surplus_reduction_mw, actual_surplus),
        "curtailment_reduction_mw": _relative_deviation(expected.expected_curtailment_reduction_mw, actual_curtailment),
        "utilization_improvement_ratio": _relative_deviation(expected.expected_utilization_improvement_ratio, actual_utilization),
    }
    known_deviations = [value for value in deviations.values() if value is not None]
    significant = any(abs(value) > float(deviation_tolerance_ratio) for value in known_deviations) if known_deviations else None

    lesson_code = None
    lesson_signal = None
    load_dev = deviations.get("load_shift_mw")
    surplus_dev = deviations.get("surplus_reduction_mw")
    if load_dev is not None and load_dev < -deviation_tolerance_ratio:
        lesson_code = "FLEXIBLE_RESPONSE_OVER_ESTIMATED"
        lesson_signal = "The assumed price sensitivity overestimated flexible-load response."
    elif load_dev is not None and load_dev > deviation_tolerance_ratio:
        lesson_code = "FLEXIBLE_RESPONSE_UNDER_ESTIMATED"
        lesson_signal = "The assumed price sensitivity underestimated flexible-load response."
    elif surplus_dev is not None and surplus_dev < -deviation_tolerance_ratio:
        lesson_code = "SURPLUS_REDUCTION_UNDERPERFORMED"
        lesson_signal = "Solar-surplus reduction underperformed the expected Dynamic Pricing impact."
    elif significant:
        lesson_code = "DYNAMIC_PRICING_IMPACT_DEVIATION"
        lesson_signal = "Dynamic Pricing impact deviated from the expected outcome beyond the configured tolerance."

    # Full goal achievement requires all four expected-vs-actual impact metrics.
    # A material load-response deviation can still be identified from partial
    # evidence and may create a lesson, but it must not fabricate a full success.
    all_metrics_known = all(value is not None for value in deviations.values())
    if not all_metrics_known:
        goal_result = "NOT_EVALUATED"
        verification_status = "PARTIAL_EVIDENCE" if known_deviations else "NOT_EVALUATED"
    elif significant:
        zero_or_near_zero = actual_shift is not None and actual_shift <= 1e-9 and (expected.expected_load_shift_mw or 0.0) > 1e-9
        goal_result = "NOT_ACHIEVED" if zero_or_near_zero else "PARTIAL"
        verification_status = "COMPLETED"
    else:
        goal_result = "ACHIEVED"
        verification_status = "COMPLETED"

    return DynamicPricingOutcomeMetrics(
        tool_call_id=result.tool_call_id,
        verification_status=verification_status,
        goal_result=goal_result,
        expected_load_shift_mw=expected.expected_load_shift_mw,
        actual_load_shift_mw=round(actual_shift, 3) if actual_shift is not None else None,
        expected_surplus_reduction_mw=expected.expected_surplus_reduction_mw,
        actual_surplus_reduction_mw=actual_surplus,
        expected_curtailment_reduction_mw=expected.expected_curtailment_reduction_mw,
        actual_curtailment_reduction_mw=actual_curtailment,
        expected_utilization_improvement_ratio=expected.expected_utilization_improvement_ratio,
        actual_utilization_improvement_ratio=actual_utilization,
        deviations=deviations,
        deviation_tolerance_ratio=float(deviation_tolerance_ratio),
        significant_deviation=significant,
        lesson_code=lesson_code,
        lesson_signal=lesson_signal,
        evidence_basis=evidence_basis,
    )


def analyze_dynamic_pricing_core(
    request: DynamicPricingRequest,
    intervals: Iterable[DynamicPricingIntervalContext],
    *,
    config: dict[str, Any] | None = None,
) -> DynamicPricingResult:
    """Run Phase-2 deterministic Tool 17 analysis on resolved interval context.

    The caller must provide resolved authoritative interval context.  This
    function itself performs no persistence, tool registration, database reads,
    planning, approval, safety, or execution.
    """

    cfg = config or load_dynamic_pricing_config(request.config_version)
    if cfg.get("profile_version") != request.config_version:
        raise ValueError("Dynamic Pricing request/config version mismatch")
    if cfg.get("energy_scope") != "SOLAR_ONLY":
        raise ValueError("Dynamic Pricing core requires SOLAR_ONLY configuration")

    resolved = sorted(list(intervals), key=lambda item: item.interval_index)
    if not resolved:
        return DynamicPricingResult(
            snapshot_id=request.snapshot_id,
            horizon_minutes=request.horizon_minutes,
            config_version=request.config_version,
            domain_status=DomainStatus.UNKNOWN,
            action_required=None,
            total_shifted_mw=None,
            reasons=["No Dynamic Pricing interval context was supplied"],
        )

    _validate_interval_identity(resolved)
    missing = _known_context_error(resolved)
    if missing:
        return DynamicPricingResult(
            snapshot_id=request.snapshot_id,
            horizon_minutes=request.horizon_minutes,
            config_version=request.config_version,
            domain_status=DomainStatus.UNKNOWN,
            action_required=None,
            total_shifted_mw=None,
            reasons=[missing],
        )

    detections: dict[int, _SurplusDetection] = {}
    severities: dict[int, _SurplusSeverity] = {}
    prices: dict[int, _PricingSignal] = {}
    responses: dict[int, _LoadResponse] = {}
    analyses: list[DynamicPricingHourAnalysis] = []

    default_baseline = float(_pricing(cfg)["baseline_price_per_mwh"])
    configured_export = float(_surplus_config(cfg)["export_interconnection_capacity_mw"])

    for interval in resolved:
        detection = detect_solar_surplus(interval, cfg)
        severity = classify_surplus_severity(detection, cfg)
        baseline = default_baseline if interval.baseline_price_per_mwh is None else interval.baseline_price_per_mwh
        price = calculate_dynamic_price(severity, baseline, cfg)
        response = simulate_flexible_load_response(interval, price, cfg)

        detections[interval.interval_index] = detection
        severities[interval.interval_index] = severity
        prices[interval.interval_index] = price
        responses[interval.interval_index] = response

        analyses.append(
            DynamicPricingHourAnalysis(
                interval_index=interval.interval_index,
                target_time=interval.target_time,
                solar_generation_mw=detection.solar_generation_mw,
                demand_mw=detection.demand_mw,
                other_generation_mw=float(interval.other_generation_mw),
                battery_charging_headroom_mw=float(interval.battery_charging_headroom_mw),
                export_capacity_mw=(
                    configured_export
                    if interval.export_capacity_mw is None
                    else float(interval.export_capacity_mw)
                ),
                flexible_load_mw=float(interval.flexible_load_mw),
                max_shiftable_load_mw=float(interval.max_shiftable_load_mw),
                baseline_price_per_mwh=price.baseline_price_per_mwh,
                solar_surplus_mw=detection.surplus_mw,
                surplus_ratio=severity.surplus_ratio,
                curtailment_risk=detection.curtailment_risk,
                severity=severity.severity,
                discount_ratio=price.discount_ratio,
                final_price_per_mwh=price.final_price_per_mwh,
                expected_flexible_response_mw=response.expected_shifted_load_mw,
            )
        )

    shift_plan = schedule_load_shift(resolved, detections, responses, cfg)
    adjustments = _aggregate_demand_adjustments(resolved, shift_plan.entries)
    action_required = bool(shift_plan.entries)

    if action_required:
        reasons = [
            f"Scheduled {len(shift_plan.entries)} conserved flexible-load shift(s) "
            f"totaling {shift_plan.total_shifted_mw:.3f} MW"
        ]
    elif not any(item.surplus_exists for item in detections.values()):
        reasons = ["No solar surplus above the configured Dynamic Pricing threshold"]
    else:
        reasons = ["Solar surplus was detected, but no feasible flexible-load source/target pairing was available"]

    return DynamicPricingResult(
        snapshot_id=request.snapshot_id,
        horizon_minutes=request.horizon_minutes,
        config_version=request.config_version,
        hourly_analysis=analyses,
        load_shift_entries=list(shift_plan.entries),
        demand_adjustments=adjustments,
        total_shifted_mw=shift_plan.total_shifted_mw,
        expected_impact=calculate_expected_dynamic_pricing_impact(resolved, adjustments, cfg),
        action_required=action_required,
        reasons=reasons,
        warnings=[],
        domain_status=DomainStatus.OK,
    )


# =============================================================================
# Phase 3 — authoritative SolarGrid adapter + public Tool 17 boundary
# =============================================================================

TOOL_17_NAME = "analyze_dynamic_pricing"
DYNAMIC_PRICING_INTERVAL_MINUTES = 15


def _aggregate_authoritative_flexible_load(loads: list[Any]) -> tuple[float | None, float | None, list[str]]:
    """Resolve flexible-load capability from the canonical ``Load`` inventory.

    The current SolarGrid Load model exposes ``is_flexible`` but does not carry
    a separate per-load shift ceiling.  For this synthetic demo, a row explicitly
    marked ``is_flexible=True`` means its current ``p_mw`` is eligible to shift,
    subject to Tool 17's existing system-wide cap.  This is deliberately derived
    from authoritative Load rows rather than accepted from an LLM request.
    """

    if not loads:
        return None, None, ["No authoritative Load rows are available for flexible-load analysis"]

    values: list[float] = []
    for load in loads:
        p_mw = getattr(load, "p_mw", None)
        if p_mw is None:
            return None, None, [f"Load {getattr(load, 'id', '?')} has unknown p_mw"]
        p = float(p_mw)
        if p < 0:
            return None, None, [f"Load {getattr(load, 'id', '?')} has invalid negative p_mw"]
        if bool(getattr(load, "is_flexible", False)):
            values.append(p)

    flexible_mw = round(sum(values), 3)
    warnings = [
        "Flexible-load capability is derived from current Load rows; "
        "is_flexible=True means the row's p_mw is fully shiftable before the configured system cap."
    ]
    return flexible_mw, flexible_mw, warnings


def _aggregate_authoritative_battery_charge_headroom(
    batteries: list[Any],
    *,
    interval_minutes: int,
) -> tuple[float | None, list[str]]:
    """Resolve current charging headroom from canonical Battery physical state.

    SolarGrid uses positive battery power for discharge and negative power for
    charge.  Headroom is limited by both remaining charging power and remaining
    SOC energy room over one planning interval.  Unavailable batteries contribute
    zero.  Missing physical data is preserved as UNKNOWN instead of being guessed.
    """

    if interval_minutes <= 0:
        raise ValueError("Dynamic Pricing interval_minutes must be positive")
    if not batteries:
        return 0.0, []

    dt_h = interval_minutes / 60.0
    total = 0.0
    for battery in batteries:
        status = str(getattr(battery, "availability_status", "UNKNOWN") or "UNKNOWN").upper()
        if status != "AVAILABLE":
            continue

        required = {
            "capacity_mwh": getattr(battery, "capacity_mwh", None),
            "soc_pct": getattr(battery, "soc_pct", None),
            "max_soc_pct": getattr(battery, "max_soc_pct", None),
            "max_charge_mw": getattr(battery, "max_charge_mw", None),
            "efficiency": getattr(battery, "efficiency", None),
            "current_power_mw": getattr(battery, "current_power_mw", None),
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            return None, [
                f"Battery {getattr(battery, 'id', '?')} is missing authoritative fields: {', '.join(missing)}"
            ]

        capacity_mwh = float(required["capacity_mwh"])
        soc_pct = float(required["soc_pct"])
        max_soc_pct = float(required["max_soc_pct"])
        max_charge_mw = float(required["max_charge_mw"])
        efficiency = float(required["efficiency"])
        current_power_mw = float(required["current_power_mw"])

        if capacity_mwh < 0 or max_charge_mw < 0 or not (0 <= soc_pct <= 100) or not (0 <= max_soc_pct <= 100):
            return None, [f"Battery {getattr(battery, 'id', '?')} has invalid physical state"]
        if efficiency <= 0:
            return None, [f"Battery {getattr(battery, 'id', '?')} has invalid efficiency"]

        remaining_energy_room_mwh = max(0.0, (max_soc_pct - soc_pct) / 100.0 * capacity_mwh)
        energy_limited_input_mw = remaining_energy_room_mwh / (dt_h * efficiency)

        # Negative power means charging.  Existing charging consumes part of the
        # charge-power limit; discharge does not increase the declared charge limit.
        current_charge_mw = max(0.0, -current_power_mw)
        remaining_power_headroom_mw = max(0.0, max_charge_mw - current_charge_mw)
        total += min(remaining_power_headroom_mw, max(0.0, energy_limited_input_mw))

    return round(total, 3), [
        "Battery charging headroom is derived from the current authoritative Battery state "
        "and held constant across the analysis horizon until planning/execution is integrated."
    ]


def _forecast_points_for_snapshot(
    forecast_rows: list[Any],
    *,
    snapshot_time: datetime,
    horizon_minutes: int,
    interval_minutes: int,
) -> tuple[list[tuple[int, datetime, float | None, float | None]], list[str]]:
    """Return aligned future solar/demand points for T+interval ... T+horizon.

    T+0 is intentionally excluded because the existing Tool 05 dispatch contract
    targets interval END boundaries (T+15 ... T+horizon).  Phase 3 adopts the same
    timeline now so Phase 4 can connect the tools without an off-by-one semantic
    migration.
    """

    if horizon_minutes <= 0 or horizon_minutes % interval_minutes:
        raise ValueError(
            f"horizon_minutes must be positive and divisible by {interval_minutes}"
        )

    start = utc(snapshot_time)
    expected_times = [
        start + timedelta(minutes=i * interval_minutes)
        for i in range(1, horizon_minutes // interval_minutes + 1)
    ]
    by_time: dict[datetime, dict[str, float]] = {}
    duplicates: list[str] = []

    for row in forecast_rows:
        variable = str(getattr(row, "variable_name", "") or "").lower()
        if variable not in {"solar", "demand"}:
            continue
        if str(getattr(row, "unit", "") or "").upper() != "MW":
            continue
        target_time = getattr(row, "target_time", None)
        value = getattr(row, "forecast_value", None)
        if target_time is None or value is None:
            continue
        t = utc(target_time)
        if t not in expected_times:
            continue
        bucket = by_time.setdefault(t, {})
        if variable in bucket:
            duplicates.append(f"{variable}@{t.isoformat()}")
            continue
        numeric = float(value)
        if numeric < 0:
            raise ValueError(f"Negative {variable} MW forecast at {t.isoformat()}")
        bucket[variable] = numeric

    if duplicates:
        raise ValueError(
            "Ambiguous duplicate Dynamic Pricing forecasts: " + ", ".join(sorted(set(duplicates)))
        )

    missing: list[str] = []
    result: list[tuple[int, datetime, float | None, float | None]] = []
    for index, t in enumerate(expected_times):
        point = by_time.get(t, {})
        solar = point.get("solar")
        demand = point.get("demand")
        if solar is None:
            missing.append(f"solar@T+{(index + 1) * interval_minutes}")
        if demand is None:
            missing.append(f"demand@T+{(index + 1) * interval_minutes}")
        result.append((index, t, solar, demand))
    return result, missing


def resolve_authoritative_dynamic_pricing_context(
    request: DynamicPricingRequest,
    *,
    db: Any,
    models: dict[str, Any],
) -> tuple[list[DynamicPricingIntervalContext], list[str]]:
    """Resolve all Tool 17 engineering inputs from current SolarGrid storage.

    No raw engineering quantity is accepted from the LLM-facing request.  The
    request contributes only the snapshot identifier, horizon and config version.
    """

    Snapshot = models["SystemSnapshot"]
    Forecast = models["Forecast"]
    Load = models["Load"]
    Battery = models["Battery"]

    snapshot = db.session.get(Snapshot, request.snapshot_id)
    if snapshot is None:
        raise ValueError(f"Unknown snapshot_id: {request.snapshot_id}")
    if getattr(snapshot, "timestamp", None) is None:
        raise ValueError("Dynamic Pricing snapshot timestamp is unavailable")

    cfg = load_dynamic_pricing_config(request.config_version)
    source_cfg = cfg.get("authoritative_sources") or {}
    interval_minutes = int(source_cfg.get("interval_minutes", DYNAMIC_PRICING_INTERVAL_MINUTES))

    forecast_rows = db.session.query(Forecast).filter_by(snapshot_id=request.snapshot_id).all()
    points, missing_forecasts = _forecast_points_for_snapshot(
        forecast_rows,
        snapshot_time=snapshot.timestamp,
        horizon_minutes=request.horizon_minutes,
        interval_minutes=interval_minutes,
    )

    loads = db.session.query(Load).all()
    flexible_mw, max_shiftable_mw, load_warnings = _aggregate_authoritative_flexible_load(loads)

    batteries = db.session.query(Battery).all()
    battery_headroom_mw, battery_warnings = _aggregate_authoritative_battery_charge_headroom(
        batteries,
        interval_minutes=interval_minutes,
    )

    other_generation_mw = getattr(snapshot, "other_gen_mw", None)
    if other_generation_mw is not None:
        other_generation_mw = float(other_generation_mw)
        if other_generation_mw < 0:
            raise ValueError("snapshot.other_gen_mw must be non-negative")

    warnings = list(load_warnings) + list(battery_warnings)
    if missing_forecasts:
        warnings.append(
            "Missing authoritative MW forecasts: " + ", ".join(missing_forecasts)
        )

    contexts = [
        DynamicPricingIntervalContext(
            interval_index=index,
            target_time=target_time,
            solar_generation_mw=solar_mw,
            demand_mw=demand_mw,
            other_generation_mw=other_generation_mw,
            battery_charging_headroom_mw=battery_headroom_mw,
            flexible_load_mw=flexible_mw,
            max_shiftable_load_mw=max_shiftable_mw,
            export_capacity_mw=None,  # approved DP config supplies this feature assumption
            baseline_price_per_mwh=None,  # approved DP config supplies the baseline
        )
        for index, target_time, solar_mw, demand_mw in points
    ]
    return contexts, warnings


def analyze_dynamic_pricing(
    payload: DynamicPricingRequest | dict[str, Any],
    *,
    db: Any,
    models: dict[str, Any],
    run_id: int | None = None,
) -> dict[str, Any]:
    """Public Tool 17 adapter: authoritative DB context -> deterministic core.

    This function performs analysis only.  It does not create a Plan, call Tool
    05, approve anything, invoke Safety, mutate grid state or execute a load shift.
    """

    request = (
        payload
        if isinstance(payload, DynamicPricingRequest)
        else DynamicPricingRequest.model_validate(payload)
    )
    AgentRunTrace = models["AgentRunTrace"]
    ToolCallLog = models["ToolCallLog"]

    with tool_call(
        db,
        AgentRunTrace,
        ToolCallLog,
        tool_name=TOOL_17_NAME,
        tool_category="DYNAMIC_PRICING_ANALYSIS",
        input_payload=request.model_dump(mode="json"),
        run_id=run_id,
    ) as (_rid, log):
        contexts, warnings = resolve_authoritative_dynamic_pricing_context(
            request,
            db=db,
            models=models,
        )
        result = analyze_dynamic_pricing_core(request, contexts)
        result = result.model_copy(update={
            "tool_call_id": getattr(log, "id", None),
            "warnings": [*result.warnings, *warnings] if warnings else list(result.warnings),
        })
        output = result.model_dump(mode="json")
        finish_log(
            log,
            output,
            ToolStatus.SUCCESS if result.domain_status == DomainStatus.OK else ToolStatus.PARTIAL,
        )
        db.session.commit()
        return output
