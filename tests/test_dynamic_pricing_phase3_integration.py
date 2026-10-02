from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.dynamic_pricing_tools as dp
from config_loader import load_dynamic_pricing_config
from schemas import DomainStatus, DynamicPricingRequest, GeneratePlansRequest, PlanDispatch


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


class Snapshot: pass
class Forecast: pass
class Load: pass
class Battery: pass
class AgentRunTrace: pass
class ToolCallLog: pass


class FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter_by(self, **kwargs):
        return FakeQuery([
            row for row in self.rows
            if all(getattr(row, key, None) == value for key, value in kwargs.items())
        ])

    def all(self):
        return list(self.rows)


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0

    def get(self, model, key):
        for row in self.rows.get(model, []):
            if getattr(row, "id", None) == key:
                return row
        return None

    def query(self, model):
        return FakeQuery(self.rows.get(model, []))

    def commit(self):
        self.commits += 1


class FakeDB:
    def __init__(self, rows):
        self.session = FakeSession(rows)


MODELS = {
    "SystemSnapshot": Snapshot,
    "Forecast": Forecast,
    "Load": Load,
    "Battery": Battery,
    "AgentRunTrace": AgentRunTrace,
    "ToolCallLog": ToolCallLog,
}


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


def forecast(snapshot_id, offset, variable, value, unit="MW"):
    return ns(
        snapshot_id=snapshot_id,
        target_time=NOW + timedelta(minutes=offset),
        variable_name=variable,
        forecast_value=value,
        unit=unit,
    )


def base_rows(*, flexible=True, missing_t30_demand=False, battery_missing=False):
    snapshot = ns(id=7, timestamp=NOW, other_gen_mw=0.0)
    forecasts = [
        # T+0 exists in the authoritative forecast contract but Tool 17 must align
        # to the same future END boundaries later consumed by Tool 05.
        forecast(7, 0, "solar", 999.0),
        forecast(7, 0, "demand", 999.0),
        forecast(7, 15, "solar", 20.0),
        forecast(7, 15, "demand", 200.0),
        forecast(7, 30, "solar", 300.0),
    ]
    if not missing_t30_demand:
        forecasts.append(forecast(7, 30, "demand", 50.0))

    loads = [
        ns(id=1, p_mw=100.0, is_flexible=False),
        ns(id=2, p_mw=40.0, is_flexible=flexible),
    ]
    battery = ns(
        id=1,
        availability_status="AVAILABLE",
        capacity_mwh=60.0,
        soc_pct=50.0,
        max_soc_pct=95.0,
        max_charge_mw=20.0,
        efficiency=None if battery_missing else 0.9,
        current_power_mw=0.0,
    )
    return {
        Snapshot: [snapshot],
        Forecast: forecasts,
        Load: loads,
        Battery: [battery],
    }


def patch_runtime_logging(monkeypatch):
    @contextmanager
    def fake_tool_call(*args, **kwargs):
        yield 123, SimpleNamespace(id=321)

    monkeypatch.setattr(dp, "tool_call", fake_tool_call)
    monkeypatch.setattr(dp, "finish_log", lambda *args, **kwargs: None)


def test_phase3_config_declares_authoritative_solar_only_sources():
    cfg = load_dynamic_pricing_config("v1.0-demo")
    assert cfg["energy_scope"] == "SOLAR_ONLY"
    assert cfg["required_forecast_variables"] == ["solar", "demand"]
    sources = cfg["authoritative_sources"]
    assert sources["forecast_variables"] == ["solar", "demand"]
    assert sources["flexible_load_source"] == "LOAD_ROWS_WITH_IS_FLEXIBLE_TRUE"
    assert sources["battery_headroom_source"] == "BATTERY_ROWS_PHYSICAL_STATE_AND_SOC"
    assert sources["request_raw_engineering_values_allowed"] is False
    assert "wind" not in json.dumps(cfg).lower()


def test_resolver_uses_snapshot_forecasts_load_inventory_and_battery_state():
    db = FakeDB(base_rows())
    request = DynamicPricingRequest(snapshot_id=7, horizon_minutes=30)
    contexts, warnings = dp.resolve_authoritative_dynamic_pricing_context(
        request, db=db, models=MODELS
    )

    assert len(contexts) == 2
    assert contexts[0].target_time == NOW + timedelta(minutes=15)
    assert contexts[0].solar_generation_mw == pytest.approx(20.0)
    assert contexts[0].demand_mw == pytest.approx(200.0)
    assert contexts[1].target_time == NOW + timedelta(minutes=30)
    assert contexts[1].solar_generation_mw == pytest.approx(300.0)
    assert contexts[1].demand_mw == pytest.approx(50.0)
    # T+0=999 must not leak into future interval analysis.
    assert all(x.solar_generation_mw != 999.0 for x in contexts)
    assert all(x.demand_mw != 999.0 for x in contexts)

    assert all(x.flexible_load_mw == pytest.approx(40.0) for x in contexts)
    assert all(x.max_shiftable_load_mw == pytest.approx(40.0) for x in contexts)
    # 45% SOC room on a 60 MWh battery is energy-rich enough that 20 MW max
    # charge power is the binding one-interval limit.
    assert all(x.battery_charging_headroom_mw == pytest.approx(20.0) for x in contexts)
    assert all(x.other_generation_mw == pytest.approx(0.0) for x in contexts)
    assert warnings


def test_public_tool17_runs_from_context_identifiers_only(monkeypatch):
    patch_runtime_logging(monkeypatch)
    db = FakeDB(base_rows())

    output = dp.analyze_dynamic_pricing(
        {"snapshot_id": 7, "horizon_minutes": 30, "config_version": "v1.0-demo"},
        db=db,
        models=MODELS,
        run_id=None,
    )

    assert output["snapshot_id"] == 7
    assert output["tool_call_id"] == 321
    assert output["energy_scope"] == "SOLAR_ONLY"
    assert output["domain_status"] == DomainStatus.OK.value
    assert output["action_required"] is True
    assert output["total_shifted_mw"] > 0
    assert len(output["load_shift_entries"]) > 0
    assert db.session.commits == 1


def test_missing_linked_forecast_preserves_unknown_not_zero(monkeypatch):
    patch_runtime_logging(monkeypatch)
    db = FakeDB(base_rows(missing_t30_demand=True))
    output = dp.analyze_dynamic_pricing(
        {"snapshot_id": 7, "horizon_minutes": 30},
        db=db,
        models=MODELS,
    )
    assert output["domain_status"] == DomainStatus.UNKNOWN.value
    assert output["action_required"] is None
    assert output["total_shifted_mw"] is None
    assert any("demand@T+30" in warning for warning in output["warnings"])


def test_existing_nonflexible_load_inventory_is_known_zero_flexible_capability(monkeypatch):
    patch_runtime_logging(monkeypatch)
    rows = base_rows(flexible=False)
    db = FakeDB(rows)
    output = dp.analyze_dynamic_pricing(
        {"snapshot_id": 7, "horizon_minutes": 30}, db=db, models=MODELS
    )
    assert output["domain_status"] == DomainStatus.OK.value
    assert output["action_required"] is False
    assert output["total_shifted_mw"] == pytest.approx(0.0)
    assert all(x["flexible_load_mw"] == pytest.approx(0.0) for x in output["hourly_analysis"])


def test_missing_battery_physical_state_is_unknown(monkeypatch):
    patch_runtime_logging(monkeypatch)
    db = FakeDB(base_rows(battery_missing=True))
    output = dp.analyze_dynamic_pricing(
        {"snapshot_id": 7, "horizon_minutes": 30}, db=db, models=MODELS
    )
    assert output["domain_status"] == DomainStatus.UNKNOWN.value
    assert output["action_required"] is None
    assert any("Battery 1" in warning for warning in output["warnings"])


def test_no_battery_rows_is_authoritative_zero_headroom():
    rows = base_rows()
    rows[Battery] = []
    db = FakeDB(rows)
    contexts, _ = dp.resolve_authoritative_dynamic_pricing_context(
        DynamicPricingRequest(snapshot_id=7, horizon_minutes=30),
        db=db,
        models=MODELS,
    )
    assert all(item.battery_charging_headroom_mw == 0.0 for item in contexts)


def test_tool17_rejects_unknown_snapshot():
    db = FakeDB({Snapshot: [], Forecast: [], Load: [], Battery: []})
    with pytest.raises(ValueError, match="Unknown snapshot_id"):
        dp.resolve_authoritative_dynamic_pricing_context(
            DynamicPricingRequest(snapshot_id=99, horizon_minutes=30),
            db=db,
            models=MODELS,
        )


def test_phase3_registers_tool17_but_does_not_connect_it_to_tool05():
    registry = (ROOT / "tool_registry.py").read_text(encoding="utf-8")
    dispatcher = (ROOT / "agents" / "tool_dispatcher.py").read_text(encoding="utf-8")
    dp_source = (ROOT / "tools" / "dynamic_pricing_tools.py").read_text(encoding="utf-8")

    assert '"analyze_dynamic_pricing": analyze_dynamic_pricing' in registry
    assert "TOOLS_01_17" in registry
    assert "TOOLS_01_16 =" in registry  # compatibility view preserved
    assert '"analyze_dynamic_pricing": DynamicPricingRequest' in dispatcher
    assert 'if name == "analyze_dynamic_pricing"' in dispatcher
    assert '"analyze_dynamic_pricing": "DYNAMIC_PRICING"' in dispatcher

    # Phase 3 boundary: the DP implementation does not import/call Tool 05.
    assert "generate_and_optimize_plans" not in dp_source
    assert "tools.planning_tools" not in dp_source


def test_legacy_planning_path_still_has_no_active_dynamic_pricing_context():
    request = GeneratePlansRequest(snapshot_id=7, horizon_minutes=30, required_candidate_count=4)
    assert request.dynamic_pricing_context is None
    assert "dynamic_pricing" not in PlanDispatch.model_fields
