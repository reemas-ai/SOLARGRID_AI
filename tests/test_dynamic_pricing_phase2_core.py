from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from config_loader import load_dynamic_pricing_config
from schemas import DomainStatus, DynamicPricingRequest, DynamicPricingSeverity
from tools.dynamic_pricing_tools import (
    DynamicPricingIntervalContext,
    analyze_dynamic_pricing_core,
    calculate_dynamic_price,
    classify_surplus_severity,
    detect_solar_surplus,
    schedule_load_shift,
    simulate_flexible_load_response,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
CFG = load_dynamic_pricing_config("v1.0-demo")


def interval(
    index: int,
    *,
    solar: float | None = 0.0,
    demand: float | None = 100.0,
    other: float | None = 20.0,
    battery_headroom: float | None = 10.0,
    export_capacity: float | None = 5.0,
    flexible: float | None = 20.0,
    max_shiftable: float | None = 20.0,
    baseline_price: float | None = 60.0,
) -> DynamicPricingIntervalContext:
    return DynamicPricingIntervalContext(
        interval_index=index,
        target_time=NOW + timedelta(minutes=15 * index),
        solar_generation_mw=solar,
        demand_mw=demand,
        other_generation_mw=other,
        battery_charging_headroom_mw=battery_headroom,
        export_capacity_mw=export_capacity,
        flexible_load_mw=flexible,
        max_shiftable_load_mw=max_shiftable,
        baseline_price_per_mwh=baseline_price,
    )


def request(horizon_minutes: int = 60) -> DynamicPricingRequest:
    return DynamicPricingRequest(snapshot_id=7, horizon_minutes=horizon_minutes)


def test_phase2_implementation_is_solar_only_by_contract_and_source():
    source = (ROOT / "tools" / "dynamic_pricing_tools.py").read_text(encoding="utf-8").lower()
    assert "wind_generation" not in source
    assert "total_renewable" not in source
    assert "renewable_mw" not in source


def test_detect_solar_surplus_preserves_standalone_absorption_semantics():
    item = interval(
        0,
        solar=180.0,
        demand=140.0,
        other=40.0,
        battery_headroom=20.0,
        export_capacity=10.0,
    )
    result = detect_solar_surplus(item, CFG)
    # net demand headroom=100, extra absorption=30 => 180 - 130 = 50 MW.
    assert result.surplus_mw == pytest.approx(50.0)
    assert result.surplus_exists is True


def test_more_solar_never_reduces_detected_surplus():
    low = detect_solar_surplus(interval(0, solar=100), CFG)
    high = detect_solar_surplus(interval(0, solar=300), CFG)
    assert high.surplus_mw >= low.surplus_mw


def test_more_demand_or_absorption_never_increases_surplus():
    base = detect_solar_surplus(interval(0, solar=300, demand=50, battery_headroom=5, export_capacity=5), CFG)
    more_demand = detect_solar_surplus(interval(0, solar=300, demand=250, battery_headroom=5, export_capacity=5), CFG)
    more_battery = detect_solar_surplus(interval(0, solar=300, demand=50, battery_headroom=200, export_capacity=5), CFG)
    more_export = detect_solar_surplus(interval(0, solar=300, demand=50, battery_headroom=5, export_capacity=200), CFG)
    assert more_demand.surplus_mw <= base.surplus_mw
    assert more_battery.surplus_mw <= base.surplus_mw
    assert more_export.surplus_mw <= base.surplus_mw


def test_severity_thresholds_are_preserved():
    # Build exact surplus amounts by using zero demand headroom/absorption.
    values = [
        (10.0, DynamicPricingSeverity.LOW),
        (30.0, DynamicPricingSeverity.MEDIUM),
        (70.0, DynamicPricingSeverity.HIGH),
        (120.0, DynamicPricingSeverity.CRITICAL),
    ]
    for solar, expected in values:
        detection = detect_solar_surplus(
            interval(0, solar=solar, demand=0, other=0, battery_headroom=0, export_capacity=0),
            CFG,
        )
        severity = classify_surplus_severity(detection, CFG)
        assert severity.severity == expected


def test_dynamic_price_is_deterministic_capped_and_floored():
    detection = detect_solar_surplus(
        interval(0, solar=10_000, demand=0, other=0, battery_headroom=0, export_capacity=0),
        CFG,
    )
    severity = classify_surplus_severity(detection, CFG)
    p1 = calculate_dynamic_price(severity, 60.0, CFG)
    p2 = calculate_dynamic_price(severity, 60.0, CFG)
    assert p1 == p2
    assert p1.discount_ratio <= CFG["pricing"]["max_discount_ratio"] + 1e-9
    assert p1.final_price_per_mwh >= CFG["pricing"]["min_price_per_mwh"] - 1e-9
    assert p1.final_price_per_mwh >= 0


def test_price_floor_keeps_reported_discount_internally_consistent():
    custom = {
        **CFG,
        "pricing": {**CFG["pricing"], "min_price_per_mwh": 55.0},
    }
    detection = detect_solar_surplus(
        interval(0, solar=1000, demand=0, other=0, battery_headroom=0, export_capacity=0),
        custom,
    )
    severity = classify_surplus_severity(detection, custom)
    price = calculate_dynamic_price(severity, 60.0, custom)
    assert price.final_price_per_mwh == pytest.approx(55.0)
    assert price.discount_ratio == pytest.approx(1.0 - 55.0 / 60.0, abs=1e-4)


def test_flexible_response_is_bounded_by_available_and_shiftable_load():
    item = interval(0, flexible=15.0, max_shiftable=1000.0)
    detection = detect_solar_surplus(
        interval(0, solar=1000, demand=0, other=0, battery_headroom=0, export_capacity=0,
                 flexible=15.0, max_shiftable=1000.0),
        CFG,
    )
    severity = classify_surplus_severity(detection, CFG)
    price = calculate_dynamic_price(severity, 60.0, CFG)
    response = simulate_flexible_load_response(item, price, CFG)
    assert 0 <= response.expected_shifted_load_mw <= 15.0


def test_zero_discount_or_zero_flexible_load_yields_zero_response():
    no_surplus = interval(0, solar=0, flexible=100, max_shiftable=100)
    detection = detect_solar_surplus(no_surplus, CFG)
    severity = classify_surplus_severity(detection, CFG)
    price = calculate_dynamic_price(severity, 60.0, CFG)
    assert simulate_flexible_load_response(no_surplus, price, CFG).expected_shifted_load_mw == 0

    target = interval(1, solar=1000, demand=0, other=0, battery_headroom=0, export_capacity=0,
                      flexible=0, max_shiftable=0)
    detection2 = detect_solar_surplus(target, CFG)
    severity2 = classify_surplus_severity(detection2, CFG)
    price2 = calculate_dynamic_price(severity2, 60.0, CFG)
    assert simulate_flexible_load_response(target, price2, CFG).expected_shifted_load_mw == 0


def test_end_to_end_core_builds_conserved_load_shift_and_adjustments():
    intervals = [
        interval(0, solar=20, demand=220, other=40, battery_headroom=5, export_capacity=5,
                 flexible=80, max_shiftable=60),
        interval(1, solar=350, demand=120, other=40, battery_headroom=20, export_capacity=10,
                 flexible=60, max_shiftable=40),
        interval(2, solar=15, demand=210, other=40, battery_headroom=5, export_capacity=5,
                 flexible=50, max_shiftable=50),
        interval(3, solar=300, demand=130, other=40, battery_headroom=15, export_capacity=10,
                 flexible=50, max_shiftable=35),
    ]
    result = analyze_dynamic_pricing_core(request(), intervals)
    assert result.domain_status == DomainStatus.OK
    assert result.action_required is True
    assert result.total_shifted_mw is not None and result.total_shifted_mw > 0
    assert len(result.load_shift_entries) > 0
    assert sum(x.demand_delta_mw for x in result.demand_adjustments) == pytest.approx(0.0)
    shifted_in = sum(max(x.demand_delta_mw, 0) for x in result.demand_adjustments)
    shifted_out = sum(max(-x.demand_delta_mw, 0) for x in result.demand_adjustments)
    assert shifted_in == pytest.approx(shifted_out)
    assert result.total_shifted_mw == pytest.approx(shifted_in)


def test_system_wide_shift_cap_is_enforced():
    custom = {
        **CFG,
        "flexible_load": {**CFG["flexible_load"], "max_flexible_shift_mw": 3.0},
    }
    intervals = [
        interval(0, solar=0, demand=200, flexible=200, max_shiftable=200),
        interval(1, solar=500, demand=50, other=10, battery_headroom=0, export_capacity=0,
                 flexible=200, max_shiftable=200),
    ]
    result = analyze_dynamic_pricing_core(request(30), intervals, config=custom)
    assert result.total_shifted_mw is not None
    assert result.total_shifted_mw <= 3.0 + 1e-9


def test_no_surplus_returns_known_no_action_not_unknown():
    intervals = [
        interval(0, solar=20, demand=300),
        interval(1, solar=25, demand=300),
    ]
    result = analyze_dynamic_pricing_core(request(30), intervals)
    assert result.domain_status == DomainStatus.OK
    assert result.action_required is False
    assert result.total_shifted_mw == 0.0
    assert result.load_shift_entries == []
    assert result.demand_adjustments == []


def test_missing_required_context_preserves_unknown_instead_of_zero():
    intervals = [interval(0, solar=None)]
    result = analyze_dynamic_pricing_core(request(15), intervals)
    assert result.domain_status == DomainStatus.UNKNOWN
    assert result.action_required is None
    assert result.total_shifted_mw is None
    assert result.hourly_analysis == []
    assert "solar_generation_mw" in result.reasons[0]


def test_missing_battery_headroom_is_unknown_but_export_can_use_configured_capacity():
    unknown = analyze_dynamic_pricing_core(request(15), [interval(0, battery_headroom=None)])
    assert unknown.domain_status == DomainStatus.UNKNOWN

    known = analyze_dynamic_pricing_core(
        request(15),
        [interval(0, solar=300, demand=50, other=10, battery_headroom=0, export_capacity=None)],
    )
    assert known.domain_status == DomainStatus.OK
    assert known.hourly_analysis[0].export_capacity_mw == pytest.approx(
        CFG["surplus_detection"]["export_interconnection_capacity_mw"]
    )


def test_baseline_price_falls_back_to_feature_config():
    result = analyze_dynamic_pricing_core(
        request(15),
        [interval(0, solar=300, demand=50, other=10, battery_headroom=0,
                  export_capacity=0, baseline_price=None)],
    )
    assert result.hourly_analysis[0].baseline_price_per_mwh == pytest.approx(
        CFG["pricing"]["baseline_price_per_mwh"]
    )


def test_duplicate_interval_identity_is_rejected():
    a = interval(0)
    b = DynamicPricingIntervalContext(
        interval_index=0,
        target_time=NOW + timedelta(minutes=15),
        solar_generation_mw=10,
        demand_mw=100,
        other_generation_mw=20,
        battery_charging_headroom_mw=10,
        flexible_load_mw=20,
        max_shiftable_load_mw=20,
        export_capacity_mw=5,
        baseline_price_per_mwh=60,
    )
    with pytest.raises(ValueError, match="interval indexes must be unique"):
        analyze_dynamic_pricing_core(request(30), [a, b])


def test_phase2_core_remains_independent_of_tool05_after_later_registration():
    # Phase 3 may register the already-tested core as Tool 17, but the Phase 2
    # deterministic implementation must remain independent of Tool 05/planning.
    source = (ROOT / "tools" / "dynamic_pricing_tools.py").read_text(encoding="utf-8")
    assert "generate_and_optimize_plans" not in source
    assert "tools.planning_tools" not in source


def test_max_target_hours_is_preserved_across_fifteen_minute_intervals():
    custom = {
        **CFG,
        "flexible_load": {**CFG["flexible_load"], "max_target_hours": 1},
    }
    intervals = [
        interval(0, solar=0, demand=300, flexible=500, max_shiftable=500),
    ]
    for i in range(1, 9):
        intervals.append(
            interval(
                i,
                solar=500 + i,
                demand=50,
                other=10,
                battery_headroom=0,
                export_capacity=0,
                flexible=100,
                max_shiftable=100,
            )
        )
    result = analyze_dynamic_pricing_core(request(135), intervals, config=custom)
    target_indexes = {entry.target_interval_index for entry in result.load_shift_entries}
    # 1 configured target hour == at most four 15-minute target intervals.
    assert len(target_indexes) <= 4
