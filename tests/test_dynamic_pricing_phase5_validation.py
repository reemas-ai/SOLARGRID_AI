from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.safety_agent import SafetyAgent, CheckStatus, Reason
from schemas import (
    DomainStatus,
    DynamicPricingResult,
    PlanDispatch,
    DispatchInterval,
    PlanStatus,
)
from tools import planning_tools as pt

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def dp_output(tool_call_id=41):
    return DynamicPricingResult(
        tool_call_id=tool_call_id,
        snapshot_id=7,
        horizon_minutes=30,
        config_version="v1.0-demo",
        energy_scope="SOLAR_ONLY",
        demand_adjustments=[
            {"interval_index": 0, "target_time": NOW + timedelta(minutes=15), "demand_delta_mw": -20.0},
            {"interval_index": 1, "target_time": NOW + timedelta(minutes=30), "demand_delta_mw": 20.0},
        ],
        total_shifted_mw=20.0,
        action_required=True,
        domain_status=DomainStatus.OK,
    ).model_dump(mode="json")


class ExprField:
    def __eq__(self, other): return self
    def __ge__(self, other): return self
    def __le__(self, other): return self
    def in_(self, other): return self
    def desc(self): return self


class Query:
    def __init__(self, rows): self.rows = list(rows)
    def filter(self, *args): return self
    def filter_by(self, **kwargs):
        return Query([row for row in self.rows if all(getattr(row, k, None) == v for k, v in kwargs.items())])
    def order_by(self, *args): return self
    def all(self): return list(self.rows)


class SnapshotModel: pass
class ForecastModel:
    snapshot_id = ExprField()
    target_time = ExprField()
class PlanModel:
    id = ExprField()
class GeneratorModel:
    id = ExprField()
class ReserveAssessmentModel:
    id = ExprField()
class AgentRunTraceModel: pass
class ToolCallLogModel: pass


def make_dispatch(with_dp=True):
    return PlanDispatch(
        interval_minutes=15,
        start_time=NOW,
        intervals=[
            DispatchInterval(index=0, start=NOW, generators={}, batteries={}, solar_curtailment_mw=0.0, residual_imbalance_mw=0.0),
            DispatchInterval(index=1, start=NOW + timedelta(minutes=15), generators={}, batteries={}, solar_curtailment_mw=0.0, residual_imbalance_mw=0.0),
        ],
        dynamic_pricing_context={"tool_call_id": 41} if with_dp else None,
    )


def make_common_rows(with_dp=True):
    snapshot = SimpleNamespace(id=7, timestamp=NOW, demand_mw=123.0, state_json={"generators": [], "batteries": []})
    forecasts = [
        SimpleNamespace(snapshot_id=7, target_time=NOW + timedelta(minutes=15), variable_name="demand", forecast_value=200.0, unit="MW"),
        SimpleNamespace(snapshot_id=7, target_time=NOW + timedelta(minutes=30), variable_name="demand", forecast_value=50.0, unit="MW"),
    ]
    plan = SimpleNamespace(
        id=99,
        snapshot_id=7,
        actions=make_dispatch(with_dp=with_dp).model_dump(mode="json"),
        status="PROPOSED",
        assumptions="",
    )
    log = SimpleNamespace(
        id=41,
        tool_name="analyze_dynamic_pricing",
        status="SUCCESS",
        input_json={"snapshot_id": 7, "horizon_minutes": 30, "config_version": "v1.0-demo"},
        output_json=dp_output(),
    )
    return snapshot, forecasts, plan, log


class Session:
    def __init__(self, snapshot, plan, log, reserve=None):
        self.snapshot = snapshot
        self.plan = plan
        self.log = log
        self.reserve = reserve
        self.added = []
    def get(self, model, ident):
        if model is SnapshotModel and ident == 7: return self.snapshot
        if model is PlanModel and ident == 99: return self.plan
        if model is ToolCallLogModel and ident == 41: return self.log
        if model is ReserveAssessmentModel and self.reserve is not None and ident == self.reserve.id: return self.reserve
        return None
    def add(self, obj): self.added.append(obj)
    def flush(self): pass
    def commit(self): pass
    def rollback(self): pass


@contextmanager
def fake_tool_call(*args, **kwargs):
    yield 501, SimpleNamespace()


def fake_network_case():
    net = SimpleNamespace()
    net.solargrid_mapping = {
        "generators": {},
        "batteries": {},
        "loads": {"L1": {"element": 0, "p_mw": 100.0}},
        "voltage_limits_pu": [0.95, 1.05],
        "loading_limit_pct": 100.0,
    }
    net.load = pd.DataFrame({"p_mw": [100.0]})
    net.res_bus = pd.DataFrame({"vm_pu": [1.0]})
    net.res_line = pd.DataFrame({"loading_percent": [10.0]})
    net.res_trafo = pd.DataFrame({"loading_percent": []})
    net.converged = True
    return net


def run_tool08(monkeypatch, *, with_dp=True):
    snapshot, forecasts, plan, log = make_common_rows(with_dp=with_dp)
    ForecastModel.query = Query(forecasts)
    db = SimpleNamespace(session=Session(snapshot, plan, log))
    models = {
        "Plan": PlanModel,
        "SystemSnapshot": SnapshotModel,
        "Forecast": ForecastModel,
        "AgentRunTrace": AgentRunTraceModel,
        "ToolCallLog": ToolCallLogModel,
    }
    observed = []
    fake_pp = SimpleNamespace()
    def runpp(net, init="auto"):
        observed.append(float(net.load["p_mw"].sum()))
        net.converged = True
        net.res_bus = pd.DataFrame({"vm_pu": [1.0]})
        net.res_line = pd.DataFrame({"loading_percent": [10.0]})
        net.res_trafo = pd.DataFrame({"loading_percent": []})
    fake_pp.runpp = runpp
    monkeypatch.setitem(sys.modules, "pandapower", fake_pp)
    monkeypatch.setattr(pt, "_case", lambda network_id: fake_network_case())
    monkeypatch.setattr(pt, "tool_call", fake_tool_call)
    monkeypatch.setattr(pt, "finish_log", lambda *args, **kwargs: None)
    result = pt.run_power_flow(
        {"plan_id": 99, "network_id": "TEST", "context_snapshot_id": 7},
        db=db, models=models,
    )
    return result, observed


def test_plan_dispatch_persists_only_trusted_tool17_reference_and_legacy_remains_valid():
    legacy = make_dispatch(with_dp=False)
    dp = make_dispatch(with_dp=True)
    assert legacy.dynamic_pricing_context is None
    assert dp.dynamic_pricing_context.tool_call_id == 41
    assert "demand_adjustments" not in dp.model_dump(mode="json")["dynamic_pricing_context"]


def test_tool08_uses_exact_effective_demand_profile_for_dp_plan(monkeypatch):
    result, observed = run_tool08(monkeypatch, with_dp=True)
    assert observed == pytest.approx([180.0, 70.0])
    assert result["validated_demand_mw"] == pytest.approx([180.0, 70.0])
    assert result["dynamic_pricing_tool_call_id"] == 41
    assert result["is_network_feasible"] is True


def test_tool08_legacy_plan_preserves_snapshot_demand_behavior(monkeypatch):
    result, observed = run_tool08(monkeypatch, with_dp=False)
    assert observed == pytest.approx([123.0, 123.0])
    assert result["validated_demand_mw"] == []
    assert result["dynamic_pricing_tool_call_id"] is None


def build_tool09_env(monkeypatch, *, pf_profile=(180.0, 70.0), pf_tool_call_id=41):
    snapshot, forecasts, plan, log = make_common_rows(with_dp=True)
    reserve = SimpleNamespace(
        id=55, snapshot_id=7, time_horizon_minutes=30,
        required_reserve_mw=10.0, actual_reserve_mw=20.0,
    )
    ForecastModel.query = Query(forecasts)
    PlanModel.query = Query([plan])
    GeneratorModel.query = Query([])
    ReserveAssessmentModel.query = Query([reserve])

    class PlanEvaluationModel:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    db = SimpleNamespace(session=Session(snapshot, plan, log, reserve=reserve))
    models = {
        "Plan": PlanModel,
        "Generator": GeneratorModel,
        "ReserveAssessment": ReserveAssessmentModel,
        "SystemSnapshot": SnapshotModel,
        "Forecast": ForecastModel,
        "AgentRunTrace": AgentRunTraceModel,
        "ToolCallLog": ToolCallLogModel,
        "PlanEvaluation": PlanEvaluationModel,
    }
    monkeypatch.setattr(pt, "tool_call", fake_tool_call)
    monkeypatch.setattr(pt, "finish_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(pt, "load_decision_policy", lambda version: {
        "status": "APPROVED",
        "network_validation_required": False,
        "network_id": "TEST",
        "hard_constraints": {"residual_imbalance_tolerance_mw": 0.5},
        "cost_model": {"generation_cost_usd_per_mwh": {}, "ramping_wear_usd_per_mw_ramp": 0.0},
    })
    monkeypatch.setattr(pt, "check_generator_constraints", lambda *a, **k: {"aggregate_feasible": True, "generators": []})
    monkeypatch.setattr(pt, "check_battery_constraints", lambda *a, **k: {"aggregate_feasible": True, "batteries": []})
    monkeypatch.setattr(pt, "run_power_flow", lambda *a, **k: {
        "plan_id": 99,
        "network_id": "TEST",
        "status": "CONVERGED",
        "converged": True,
        "is_network_feasible": True,
        "validated_demand_mw": list(pf_profile),
        "dynamic_pricing_tool_call_id": pf_tool_call_id,
        "thermal_violations": [], "voltage_violations": [], "congestion": [],
        "solver_warnings": [], "solver_errors": [],
    })
    monkeypatch.setattr(pt, "_cost", lambda *a, **k: 0.0)
    monkeypatch.setattr(pt, "_residual_metrics", lambda *a, **k: (0.0, 0.0))
    return db, models


def test_tool09_recomputes_and_exposes_same_effective_demand_as_tool05_and_tool08(monkeypatch):
    db, models = build_tool09_env(monkeypatch)
    result = pt.evaluate_plans(
        {
            "plan_ids": [99],
            "decision_policy_version": "v1.0-demo",
            "reserve_assessment_id": 55,
            "context_snapshot_id": 7,
            "network_id": "TEST",
            "horizon_minutes": 30,
        },
        db=db, models=models,
    )
    ev = result["evaluations"][0]
    assert ev["validated_demand_mw"] == pytest.approx([180.0, 70.0])
    assert ev["dynamic_pricing_tool_call_id"] == 41
    assert ev["is_feasible"] is True


def test_tool09_fails_closed_if_tool08_validated_a_different_demand_profile(monkeypatch):
    db, models = build_tool09_env(monkeypatch, pf_profile=(200.0, 50.0))
    with pytest.raises(ValueError, match="effective demand profile does not match"):
        pt.evaluate_plans(
            {
                "plan_ids": [99],
                "decision_policy_version": "v1.0-demo",
                "reserve_assessment_id": 55,
                "context_snapshot_id": 7,
                "network_id": "TEST",
                "horizon_minutes": 30,
            },
            db=db, models=models,
        )


def test_safety_lifecycle_allows_approved_dp_plan_after_phase6_execution_integration():
    agent = SafetyAgent(data_source=SimpleNamespace())
    dp_plan = SimpleNamespace(
        status=PlanStatus.APPROVED.value,
        actions=make_dispatch(with_dp=True).model_dump(mode="json"),
    )
    check = agent._check_lifecycle({"plan": dp_plan})
    assert check.status is CheckStatus.PASS

    legacy_plan = SimpleNamespace(
        status=PlanStatus.APPROVED.value,
        actions=make_dispatch(with_dp=False).model_dump(mode="json"),
    )
    legacy = agent._check_lifecycle({"plan": legacy_plan})
    assert legacy.status is CheckStatus.PASS
