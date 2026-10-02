from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from pydantic import ValidationError

from config_loader import load_dynamic_pricing_config
from schemas import (
    DomainStatus,
    DynamicPricingDemandAdjustment,
    DynamicPricingHourAnalysis,
    DynamicPricingLoadShiftEntry,
    DynamicPricingRequest,
    DynamicPricingResult,
    GeneratePlansRequest,
    PlanDispatch,
)


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def test_dynamic_pricing_config_is_solar_only_and_contains_no_wind_contract():
    cfg = load_dynamic_pricing_config("v1.0-demo")
    assert cfg["energy_scope"] == "SOLAR_ONLY"
    encoded = json.dumps(cfg).lower()
    assert "wind" not in encoded
    assert cfg["pricing"]["baseline_price_per_mwh"] == 60.0
    assert cfg["pricing"]["max_discount_ratio"] == 0.70
    assert cfg["pricing"]["min_price_per_mwh"] == 5.0
    assert cfg["surplus_detection"]["surplus_threshold_mw"] == 5.0
    assert cfg["surplus_detection"]["severity_thresholds_mw"] == {
        "low": 5.0,
        "medium": 25.0,
        "high": 60.0,
        "critical": 100.0,
    }
    assert cfg["flexible_load"]["price_sensitivity"] == 1.1
    assert cfg["flexible_load"]["max_flexible_shift_mw"] == 150.0
    assert cfg["flexible_load"]["max_target_hours"] == 4


def test_dynamic_pricing_config_version_is_checked():
    with pytest.raises(ValueError, match="Unsupported Dynamic Pricing config version"):
        load_dynamic_pricing_config("not-a-real-version")


def test_tool17_request_accepts_context_only_and_rejects_raw_or_wind_values():
    request = DynamicPricingRequest(snapshot_id=7, horizon_minutes=60)
    assert request.snapshot_id == 7
    assert request.config_version == "v1.0-demo"

    with pytest.raises(ValidationError):
        DynamicPricingRequest(
            snapshot_id=7,
            horizon_minutes=60,
            solar_generation_mw=100.0,
        )

    with pytest.raises(ValidationError):
        DynamicPricingRequest(
            snapshot_id=7,
            horizon_minutes=60,
            wind_generation_mw=25.0,
        )


def test_new_public_dynamic_pricing_contracts_have_no_wind_fields():
    models = [
        DynamicPricingRequest,
        DynamicPricingHourAnalysis,
        DynamicPricingLoadShiftEntry,
        DynamicPricingDemandAdjustment,
        DynamicPricingResult,
    ]
    for model in models:
        assert all("wind" not in name.lower() for name in model.model_fields)


def test_load_shift_entry_requires_different_source_and_target():
    with pytest.raises(ValidationError, match="source and target intervals must differ"):
        DynamicPricingLoadShiftEntry(
            source_interval_index=1,
            source_time=NOW,
            target_interval_index=1,
            target_time=NOW + timedelta(minutes=15),
            shifted_mw=5.0,
        )


def test_tool17_result_accepts_conserved_solar_only_load_shift():
    result = DynamicPricingResult(
        snapshot_id=7,
        horizon_minutes=60,
        config_version="v1.0-demo",
        hourly_analysis=[],
        load_shift_entries=[
            DynamicPricingLoadShiftEntry(
                source_interval_index=0,
                source_time=NOW,
                target_interval_index=2,
                target_time=NOW + timedelta(minutes=30),
                shifted_mw=12.5,
            )
        ],
        demand_adjustments=[
            DynamicPricingDemandAdjustment(
                interval_index=0,
                target_time=NOW,
                demand_delta_mw=-12.5,
            ),
            DynamicPricingDemandAdjustment(
                interval_index=2,
                target_time=NOW + timedelta(minutes=30),
                demand_delta_mw=12.5,
            ),
        ],
        total_shifted_mw=12.5,
        action_required=True,
        domain_status=DomainStatus.OK,
    )
    assert result.energy_scope == "SOLAR_ONLY"
    assert sum(x.demand_delta_mw for x in result.demand_adjustments) == pytest.approx(0.0)


def test_tool17_result_rejects_non_conserved_demand_shift():
    with pytest.raises(ValidationError, match="must conserve total demand"):
        DynamicPricingResult(
            snapshot_id=7,
            horizon_minutes=60,
            config_version="v1.0-demo",
            demand_adjustments=[
                DynamicPricingDemandAdjustment(
                    interval_index=0,
                    target_time=NOW,
                    demand_delta_mw=-10.0,
                ),
                DynamicPricingDemandAdjustment(
                    interval_index=1,
                    target_time=NOW + timedelta(minutes=15),
                    demand_delta_mw=8.0,
                ),
            ],
            total_shifted_mw=10.0,
            action_required=True,
            domain_status=DomainStatus.OK,
        )


def test_tool17_unknown_preserves_unknown_values_instead_of_zero():
    result = DynamicPricingResult(
        snapshot_id=7,
        horizon_minutes=60,
        config_version="v1.0-demo",
        total_shifted_mw=None,
        action_required=None,
        domain_status=DomainStatus.UNKNOWN,
        reasons=["Required flexible-load forecast is unavailable"],
    )
    assert result.total_shifted_mw is None
    assert result.action_required is None


def test_existing_plan_dispatch_contract_is_unchanged_by_phase1():
    dispatch = PlanDispatch(
        interval_minutes=15,
        start_time=NOW,
        intervals=[
            {
                "index": 0,
                "start": NOW,
                "generators": {},
                "batteries": {},
                "solar_curtailment_mw": 0.0,
                "residual_imbalance_mw": 0.0,
            }
        ],
    )
    dumped = dispatch.model_dump()
    assert "dynamic_pricing" not in dumped
    assert "dynamic_pricing_context" not in dumped
    assert set(dumped) == {"schema_version", "interval_minutes", "start_time", "intervals"}


def test_legacy_generate_plans_request_remains_noop_without_dynamic_pricing_context():
    request = GeneratePlansRequest(
        snapshot_id=1,
        horizon_minutes=60,
        required_candidate_count=4,
    )
    assert request.dynamic_pricing_context is None
    assert "dynamic_pricing" not in request.model_dump(exclude_none=True)


def test_config_file_has_no_accidental_wind_key_or_value():
    raw = (ROOT / "config" / "dynamic_pricing_v1.json").read_text(encoding="utf-8").lower()
    assert "wind" not in raw
