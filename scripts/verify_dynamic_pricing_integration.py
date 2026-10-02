"""Regression check for SolarGrid Dynamic Pricing core + outcome contracts.

This script intentionally avoids Flask/pandapower so it can run as a fast
preflight even when the full Python 3.12 runtime has not been installed yet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemas import DynamicPricingRequest
from tools.dynamic_pricing_tools import (
    DynamicPricingIntervalContext,
    analyze_dynamic_pricing_core,
    evaluate_dynamic_pricing_outcome,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

contexts = [
    DynamicPricingIntervalContext(
        interval_index=0,
        target_time=NOW + timedelta(minutes=15),
        solar_generation_mw=20.0,
        demand_mw=200.0,
        other_generation_mw=40.0,
        battery_charging_headroom_mw=0.0,
        flexible_load_mw=100.0,
        max_shiftable_load_mw=100.0,
        export_capacity_mw=0.0,
        baseline_price_per_mwh=60.0,
    ),
    DynamicPricingIntervalContext(
        interval_index=1,
        target_time=NOW + timedelta(minutes=30),
        solar_generation_mw=260.0,
        demand_mw=100.0,
        other_generation_mw=40.0,
        battery_charging_headroom_mw=10.0,
        flexible_load_mw=20.0,
        max_shiftable_load_mw=20.0,
        export_capacity_mw=10.0,
        baseline_price_per_mwh=60.0,
    ),
]

result = analyze_dynamic_pricing_core(
    DynamicPricingRequest(snapshot_id=7, horizon_minutes=30, config_version="v1.0-demo"),
    contexts,
).model_copy(update={"tool_call_id": 41})

assert result.energy_scope == "SOLAR_ONLY"
assert result.action_required is True
assert result.total_shifted_mw and result.total_shifted_mw > 0
assert abs(sum(item.demand_delta_mw for item in result.demand_adjustments)) <= 1e-6
assert all(not hasattr(item, "wind_generation_mw") for item in result.hourly_analysis)

adjustments = {item.interval_index: item.demand_delta_mw for item in result.demand_adjustments}
actual_intervals = []
for analysis in result.hourly_analysis:
    before = float(analysis.demand_mw)
    after = before + float(adjustments.get(analysis.interval_index, 0.0))
    actual_intervals.append({
        "index": analysis.interval_index,
        "demand_before_dynamic_pricing_mw": before,
        "demand_after_dynamic_pricing_mw": after,
        "generators": [{"id": "solar", "actual_mw": analysis.solar_generation_mw}],
        "batteries": [],
    })

actual_actions = {
    "intervals": actual_intervals,
    "dynamic_pricing": {
        "tool_call_id": 41,
        "energy_scope": "SOLAR_ONLY",
        "response_factor": 1.0,
        "total_shifted_mw": result.total_shifted_mw,
        "conservation_verified": True,
    },
}
outcome = evaluate_dynamic_pricing_outcome(
    result,
    actual_actions,
    deviation_tolerance_ratio=0.15,
)
assert outcome.goal_result == "ACHIEVED"
assert outcome.significant_deviation is False
assert outcome.actual_load_shift_mw == result.expected_impact.expected_load_shift_mw

print(f"PASS: Tool 17 solar-only conserved shift={result.total_shifted_mw:.3f} MW")
print("PASS: exact expected-vs-actual Dynamic Pricing outcome is ACHIEVED")
