from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import simulation
from schemas import DomainStatus, DynamicPricingResult, PlanDispatch, DispatchInterval

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def dp_output(*, tool_call_id=41, include_entries=True):
    entries = []
    if include_entries:
        entries = [
            {
                "source_interval_index": 0,
                "source_time": NOW + timedelta(minutes=15),
                "target_interval_index": 1,
                "target_time": NOW + timedelta(minutes=30),
                "shifted_mw": 20.0,
            }
        ]
    return DynamicPricingResult(
        tool_call_id=tool_call_id,
        snapshot_id=7,
        horizon_minutes=30,
        config_version="v1.0-demo",
        energy_scope="SOLAR_ONLY",
        load_shift_entries=entries,
        demand_adjustments=[
            {"interval_index": 0, "target_time": NOW + timedelta(minutes=15), "demand_delta_mw": -20.0},
            {"interval_index": 1, "target_time": NOW + timedelta(minutes=30), "demand_delta_mw": 20.0},
        ],
        total_shifted_mw=20.0,
        action_required=True,
        domain_status=DomainStatus.OK,
    ).model_dump(mode="json")


def make_actions(*, with_dp=True):
    return PlanDispatch(
        interval_minutes=15,
        start_time=NOW,
        intervals=[
            DispatchInterval(index=0, start=NOW, generators={}, batteries={}, solar_curtailment_mw=0.0, residual_imbalance_mw=0.0),
            DispatchInterval(index=1, start=NOW + timedelta(minutes=15), generators={}, batteries={}, solar_curtailment_mw=0.0, residual_imbalance_mw=0.0),
        ],
        dynamic_pricing_context={"tool_call_id": 41} if with_dp else None,
    ).model_dump(mode="json")


class ExprField:
    def __init__(self, name): self.name = name
    def __eq__(self, other): return ("eq", self.name, other)


class FakeQuery:
    def __init__(self, rows): self.rows = rows
    def order_by(self, *args): return self
    def filter_by(self, **kwargs):
        return FakeQuery([r for r in self.rows if all(getattr(r, k, None) == v for k, v in kwargs.items())])
    def filter(self, *conditions):
        rows = list(self.rows)
        for condition in conditions:
            if isinstance(condition, tuple) and condition[0] == "eq":
                _, name, value = condition
                rows = [r for r in rows if getattr(r, name, None) == value]
        return FakeQuery(rows)
    def all(self): return list(self.rows)


class ActualMeasurement:
    _rows = []
    timestamp = ExprField("timestamp")
    variable_name = ExprField("variable_name")
    def __init__(self, **kwargs):
        for k, v in kwargs.items(): setattr(self, k, v)
        self.id = None


class Generator:
    id = ExprField("id")
    query = FakeQuery([])


class Battery:
    id = ExprField("id")
    query = FakeQuery([])


class Plan: pass
class SystemSnapshot: pass
class ToolCallLog: pass


class FakeSession:
    def __init__(self, objects):
        self.objects = objects
        self.next_id = 100
    def get(self, model, ident):
        return self.objects.get((model, ident))
    def add(self, obj):
        if isinstance(obj, ActualMeasurement):
            if obj.id is None:
                obj.id = self.next_id; self.next_id += 1
            ActualMeasurement._rows.append(obj)
    def flush(self): pass
    def commit(self): pass
    def rollback(self): pass


def install_fake_database(monkeypatch, *, include_entries=True, with_dp=True):
    ActualMeasurement._rows = []
    ActualMeasurement.query = FakeQuery(ActualMeasurement._rows)
    actions = make_actions(with_dp=with_dp)
    plan = SimpleNamespace(id=99, snapshot_id=7, run_id=1, actions=actions)
    snapshot = SimpleNamespace(
        id=7,
        timestamp=NOW,
        demand_mw=100.0,
        solar_gen_mw=0.0,
        other_gen_mw=0.0,
        reserve_margin_mw=10.0,
        grid_status="STABLE",
    )
    log = SimpleNamespace(
        id=41,
        tool_name="analyze_dynamic_pricing",
        status="SUCCESS",
        input_json={"snapshot_id": 7, "horizon_minutes": 30, "config_version": "v1.0-demo"},
        output_json=dp_output(include_entries=include_entries),
    )
    session = FakeSession({(Plan, 99): plan, (SystemSnapshot, 7): snapshot, (ToolCallLog, 41): log})
    db = SimpleNamespace(session=session)

    mod = ModuleType("database")
    mod.Plan = Plan
    mod.Generator = Generator
    mod.Battery = Battery
    mod.SystemSnapshot = SystemSnapshot
    mod.ActualMeasurement = ActualMeasurement
    mod.ToolCallLog = ToolCallLog
    mod.db = db
    monkeypatch.setitem(sys.modules, "database", mod)

    # Query objects must always see the current mutable measurement list.
    ActualMeasurement.query = FakeQuery(ActualMeasurement._rows)

    snapshot_counter = {"value": 1000}
    def fake_create_snapshot(**kwargs):
        snapshot_counter["value"] += 1
        return SimpleNamespace(
            id=snapshot_counter["value"],
            timestamp=kwargs["timestamp"],
            demand_mw=kwargs["demand_mw"],
            solar_gen_mw=kwargs["solar_gen_mw"],
            other_gen_mw=kwargs["other_gen_mw"],
            state_json=kwargs["state_json"],
        )
    monkeypatch.setattr(simulation, "_create_snapshot", fake_create_snapshot)
    monkeypatch.setattr(simulation, "_create_followup_forecasts", lambda **kwargs: [])

    forecast_values = {
        ("demand", NOW + timedelta(minutes=15)): 200.0,
        ("demand", NOW + timedelta(minutes=30)): 50.0,
        ("solar", NOW + timedelta(minutes=15)): 0.0,
        ("solar", NOW + timedelta(minutes=30)): 0.0,
    }
    monkeypatch.setattr(
        simulation,
        "_latest_forecast",
        lambda variable, target_time, snapshot_id=None: (
            SimpleNamespace(forecast_value=forecast_values[(variable, target_time)])
            if (variable, target_time) in forecast_values else None
        ),
    )
    simulation.reset_simulation_clock(None, seed=42)
    return plan, snapshot, log


def demand_measurements():
    return [m.actual_value for m in ActualMeasurement._rows if m.variable_name == "demand"]


def test_phase6_exact_dp_execution_applies_shift_and_preserves_total_demand(monkeypatch):
    install_fake_database(monkeypatch)
    result = simulation.apply_plan(99, simulation_options={"dynamic_pricing_response_factor": 1.0})

    assert result["execution_status"] == "SUCCESS"
    assert result["dynamic_pricing_execution"]["conservation_verified"] is True
    assert result["dynamic_pricing_execution"]["requested_total_shifted_mw"] == pytest.approx(20.0)
    assert result["dynamic_pricing_execution"]["actual_total_shifted_mw"] == pytest.approx(20.0)
    assert demand_measurements() == pytest.approx([180.0, 70.0])
    assert sum(demand_measurements()) == pytest.approx(200.0 + 50.0)
    assert result["requested_actions"]["dynamic_pricing"]["total_shifted_mw"] == pytest.approx(20.0)
    assert result["actual_actions"]["dynamic_pricing"]["total_shifted_mw"] == pytest.approx(20.0)


def test_phase6_partial_flexible_response_records_expected_vs_actual_and_conserves(monkeypatch):
    install_fake_database(monkeypatch)
    result = simulation.apply_plan(99, simulation_options={"dynamic_pricing_response_factor": 0.5})

    assert result["execution_status"] == "PARTIAL_FAIL"
    dp = result["actual_actions"]["dynamic_pricing"]
    assert dp["conservation_verified"] is True
    assert dp["total_shifted_mw"] == pytest.approx(10.0)
    assert demand_measurements() == pytest.approx([190.0, 60.0])
    assert sum(demand_measurements()) == pytest.approx(250.0)
    deviations = [d for d in result["deviations"] if d.get("asset_type") == "flexible_load_shift"]
    assert len(deviations) == 1
    assert deviations[0]["requested_mw"] == pytest.approx(20.0)
    assert deviations[0]["actual_mw"] == pytest.approx(10.0)


def test_phase6_execution_fails_closed_when_tool17_has_no_explicit_load_shift_entries(monkeypatch):
    install_fake_database(monkeypatch, include_entries=False)
    result = simulation.apply_plan(99)
    assert result["execution_status"] == "FAILED"
    assert "no executable conserved load-shift action" in result["failure_information"]
    assert ActualMeasurement._rows == []


def test_phase6_simulation_config_has_deterministic_dp_response_factor():
    cfg = simulation._load_config()
    assert cfg["dynamic_pricing_execution"]["response_factor"] == pytest.approx(1.0)


def test_phase6_legacy_execution_path_remains_unchanged_without_dp_context(monkeypatch):
    install_fake_database(monkeypatch, with_dp=False)
    result = simulation.apply_plan(99)

    assert result["execution_status"] == "SUCCESS"
    assert result["dynamic_pricing_execution"] is None
    assert "dynamic_pricing" not in result["requested_actions"]
    assert "dynamic_pricing" not in result["actual_actions"]
    assert demand_measurements() == pytest.approx([200.0, 50.0])


def test_phase6_tool13_public_contract_exposes_dp_execution_summary():
    from schemas import PlanExecutionResult, ExecutionStatus

    model = PlanExecutionResult(
        execution_id=1,
        plan_id=99,
        status=ExecutionStatus.SUCCESS,
        requested_actions={},
        actual_actions={},
        dynamic_pricing_execution={
            "tool_call_id": 41,
            "requested_total_shifted_mw": 20.0,
            "actual_total_shifted_mw": 20.0,
            "conservation_verified": True,
        },
    )
    assert model.dynamic_pricing_execution["conservation_verified"] is True


def test_phase6_tool13_persists_and_returns_dynamic_pricing_execution_evidence():
    source = (ROOT / "tools" / "operational_tools.py").read_text(encoding="utf-8")
    assert 'dynamic_pricing_execution = sim.get("dynamic_pricing_execution")' in source
    assert '"dynamic_pricing_execution": dynamic_pricing_execution' in source
    assert 'actual_actions=sim.get("actual_actions")' in source
