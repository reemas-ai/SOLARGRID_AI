from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import hashlib
import inspect
import sys

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemas import (
    DomainStatus,
    DynamicPricingResult,
    GeneratePlansRequest,
    PlanDispatch,
)
from tools import planning_tools as pt

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def dp_output(*, snapshot_id=7, horizon=30, adjustments=None, action_required=True, tool_call_id=41):
    adjustments = adjustments if adjustments is not None else [
        {
            "interval_index": 0,
            "target_time": NOW + timedelta(minutes=15),
            "demand_delta_mw": -20.0,
        },
        {
            "interval_index": 1,
            "target_time": NOW + timedelta(minutes=30),
            "demand_delta_mw": 20.0,
        },
    ]
    total = sum(max(float(x["demand_delta_mw"]), 0.0) for x in adjustments)
    return DynamicPricingResult(
        tool_call_id=tool_call_id,
        snapshot_id=snapshot_id,
        horizon_minutes=horizon,
        config_version="v1.0-demo",
        energy_scope="SOLAR_ONLY",
        hourly_analysis=[],
        load_shift_entries=[],
        demand_adjustments=adjustments,
        total_shifted_mw=total,
        action_required=action_required,
        domain_status=DomainStatus.OK,
    ).model_dump(mode="json")


class FakeSession:
    def __init__(self, rows):
        self.rows = rows

    def get(self, model, key):
        return self.rows.get((model, key))


class FakeDB:
    def __init__(self, model, log):
        self.session = FakeSession({(model, log.id): log})


class ToolCallLog: pass


def make_log(*, ident=41, tool_name="analyze_dynamic_pricing", status="SUCCESS",
             snapshot_id=7, horizon=30, output=None):
    return SimpleNamespace(
        id=ident,
        tool_name=tool_name,
        status=status,
        input_json={"snapshot_id": snapshot_id, "horizon_minutes": horizon, "config_version": "v1.0-demo"},
        output_json=output if output is not None else dp_output(snapshot_id=snapshot_id, horizon=horizon, tool_call_id=ident),
    )


def request(tool_call_id=None):
    payload = {
        "snapshot_id": 7,
        "horizon_minutes": 30,
        "required_candidate_count": 4,
    }
    if tool_call_id is not None:
        payload["dynamic_pricing_context"] = {"tool_call_id": tool_call_id}
    return GeneratePlansRequest.model_validate(payload)


def test_tool05_request_accepts_only_a_toolcall_reference_not_raw_adjustments():
    req = request(41)
    assert req.dynamic_pricing_context.tool_call_id == 41

    with pytest.raises(ValidationError):
        GeneratePlansRequest.model_validate({
            "snapshot_id": 7,
            "horizon_minutes": 30,
            "required_candidate_count": 4,
            "dynamic_pricing_context": {
                "tool_call_id": 41,
                "demand_adjustments": [{"interval_index": 0, "demand_delta_mw": 99}],
            },
        })


def test_legacy_no_context_series_is_value_equal_and_input_is_not_mutated():
    series = [
        {"target_time": NOW + timedelta(minutes=15), "solar": 100.0, "demand": 200.0},
        {"target_time": NOW + timedelta(minutes=30), "solar": 120.0, "demand": 150.0},
    ]
    original = [dict(x) for x in series]
    effective = pt._apply_dynamic_pricing_demand_adjustments(series, None)
    assert effective == original
    assert series == original
    assert effective is not series


def test_resolver_reloads_successful_tool17_output_and_checks_alignment():
    log = make_log()
    db = FakeDB(ToolCallLog, log)
    snapshot = SimpleNamespace(id=7, timestamp=NOW)
    result = pt._resolve_dynamic_pricing_planning_context(
        request(41), db=db, ToolCallLog=ToolCallLog, snapshot=snapshot, interval_minutes=15
    )
    assert result.snapshot_id == 7
    assert result.horizon_minutes == 30
    assert result.energy_scope == "SOLAR_ONLY"
    assert [x.demand_delta_mw for x in result.demand_adjustments] == [-20.0, 20.0]


def test_effective_demand_applies_only_tool17_deltas_and_conserves_total_demand():
    result = DynamicPricingResult.model_validate(dp_output())
    base = [
        {"target_time": NOW + timedelta(minutes=15), "solar": 80.0, "demand": 200.0},
        {"target_time": NOW + timedelta(minutes=30), "solar": 300.0, "demand": 50.0},
    ]
    effective = pt._apply_dynamic_pricing_demand_adjustments(base, result)

    assert effective[0]["demand"] == pytest.approx(180.0)
    assert effective[1]["demand"] == pytest.approx(70.0)
    assert effective[0]["solar"] == pytest.approx(80.0)
    assert effective[1]["solar"] == pytest.approx(300.0)
    assert sum(x["demand"] for x in effective) == pytest.approx(sum(x["demand"] for x in base))
    assert [x["dynamic_pricing_demand_delta_mw"] for x in effective] == [-20.0, 20.0]


def test_no_action_tool17_result_is_a_strict_effective_demand_noop():
    raw = dp_output(adjustments=[], action_required=False)
    result = DynamicPricingResult.model_validate(raw)
    base = [
        {"target_time": NOW + timedelta(minutes=15), "solar": 80.0, "demand": 200.0},
        {"target_time": NOW + timedelta(minutes=30), "solar": 100.0, "demand": 150.0},
    ]
    effective = pt._apply_dynamic_pricing_demand_adjustments(base, result)
    assert [x["demand"] for x in effective] == [200.0, 150.0]


@pytest.mark.parametrize(
    "log,match",
    [
        (make_log(tool_name="calculate_reserve"), "not an analyze_dynamic_pricing"),
        (make_log(status="FAILED"), "not SUCCESS"),
        (make_log(snapshot_id=8), "input snapshot does not match"),
        (make_log(horizon=60), "input horizon does not match"),
    ],
)
def test_resolver_rejects_untrusted_or_mismatched_toolcall_logs(log, match):
    db = FakeDB(ToolCallLog, log)
    with pytest.raises(ValueError, match=match):
        pt._resolve_dynamic_pricing_planning_context(
            request(log.id),
            db=db,
            ToolCallLog=ToolCallLog,
            snapshot=SimpleNamespace(id=7, timestamp=NOW),
            interval_minutes=15,
        )


def test_resolver_rejects_misaligned_adjustment_time_even_if_result_is_otherwise_valid():
    raw = dp_output(adjustments=[
        {"interval_index": 0, "target_time": NOW + timedelta(minutes=30), "demand_delta_mw": -10.0},
        {"interval_index": 1, "target_time": NOW + timedelta(minutes=15), "demand_delta_mw": 10.0},
    ])
    log = make_log(output=raw)
    db = FakeDB(ToolCallLog, log)
    with pytest.raises(ValueError, match="target_time does not align"):
        pt._resolve_dynamic_pricing_planning_context(
            request(41), db=db, ToolCallLog=ToolCallLog,
            snapshot=SimpleNamespace(id=7, timestamp=NOW), interval_minutes=15
        )


def test_negative_effective_demand_is_rejected_not_clamped_silently():
    # Build a small fake object because the public result schema correctly enforces
    # conservation, while this test targets Tool 05's final defensive bound check.
    fake = SimpleNamespace(demand_adjustments=[SimpleNamespace(interval_index=0, demand_delta_mw=-500.0)])
    with pytest.raises(ValueError, match="make demand negative"):
        pt._apply_dynamic_pricing_demand_adjustments(
            [{"target_time": NOW + timedelta(minutes=15), "solar": 1.0, "demand": 10.0}],
            fake,
        )


def test_phase4_keeps_plandispatch_contract_unchanged_and_legacy_default_inactive():
    assert "dynamic_pricing" not in PlanDispatch.model_fields
    assert request().dynamic_pricing_context is None


def test_phase4_does_not_modify_the_existing_optimizer_core():
    current = inspect.getsource(pt._build_candidate).replace("\r\n", "\n").strip().encode()
    # SHA-256 of the complete Phase 3 _build_candidate implementation. Phase 4
    # intentionally changes only the demand series prepared before this solver.
    assert hashlib.sha256(current).hexdigest() == (
        "02bd9aba49cebce6a1a43e5613fe0aa7d626582064f3595e7c6598d12296f742"
    )


def test_phase5_replaces_phase4_persistence_guard_with_canonical_context_persistence():
    source = inspect.getsource(pt.generate_and_optimize_plans)
    assert "candidate.dispatch = candidate.dispatch.model_copy" in source
    assert 'update={"dynamic_pricing_context": request.dynamic_pricing_context}' in source
    assert "_persist_candidate(db,Plan,snapshot,request,candidate,rid)" in source
    assert "analysis-only and are not persisted until DP-aware validation" not in source


def test_full_tool05_path_uses_effective_demand_only_when_trusted_context_is_present(monkeypatch):
    class ExprField:
        def __eq__(self, other): return self
        def __ge__(self, other): return self
        def __le__(self, other): return self
        def desc(self): return self

    class Query:
        def __init__(self, rows): self.rows = list(rows)
        def filter(self, *args): return self
        def filter_by(self, **kwargs):
            return Query([
                row for row in self.rows
                if all(getattr(row, key, None) == value for key, value in kwargs.items())
            ])
        def order_by(self, *args): return self
        def all(self): return list(self.rows)

    class SnapshotModel: pass
    class ForecastModel:
        snapshot_id = ExprField()
        target_time = ExprField()
    class GeneratorModel:
        id = ExprField()
    class BatteryModel:
        id = ExprField()
    class ReserveAssessmentModel:
        id = ExprField()
    class PlanModel: pass
    class AgentRunTraceModel: pass
    class ToolCallLogModel: pass

    snapshot = SimpleNamespace(
        id=7,
        timestamp=NOW,
        solar_gen_mw=50.0,
        state_json={"generators": [], "batteries": []},
    )
    forecasts = [
        SimpleNamespace(snapshot_id=7, target_time=NOW, variable_name="solar", forecast_value=50.0, unit="MW"),
        SimpleNamespace(snapshot_id=7, target_time=NOW, variable_name="demand", forecast_value=100.0, unit="MW"),
        SimpleNamespace(snapshot_id=7, target_time=NOW + timedelta(minutes=15), variable_name="solar", forecast_value=80.0, unit="MW"),
        SimpleNamespace(snapshot_id=7, target_time=NOW + timedelta(minutes=15), variable_name="demand", forecast_value=200.0, unit="MW"),
        SimpleNamespace(snapshot_id=7, target_time=NOW + timedelta(minutes=30), variable_name="solar", forecast_value=300.0, unit="MW"),
        SimpleNamespace(snapshot_id=7, target_time=NOW + timedelta(minutes=30), variable_name="demand", forecast_value=50.0, unit="MW"),
    ]
    generator = SimpleNamespace(id=1, name="G1")
    dp_log = SimpleNamespace(
        id=41,
        tool_name="analyze_dynamic_pricing",
        status="SUCCESS",
        input_json={"snapshot_id": 7, "horizon_minutes": 30, "config_version": "v1.0-demo"},
        output_json=dp_output(),
    )

    ForecastModel.query = Query(forecasts)
    GeneratorModel.query = Query([generator])
    BatteryModel.query = Query([])
    ReserveAssessmentModel.query = Query([])

    class Session:
        def __init__(self): self.commits = 0; self.rollbacks = 0
        def get(self, model, ident):
            if model is SnapshotModel and ident == 7: return snapshot
            if model is ToolCallLogModel and ident == 41: return dp_log
            return None
        def add(self, obj): pass
        def flush(self): pass
        def commit(self): self.commits += 1
        def rollback(self): self.rollbacks += 1

    db = SimpleNamespace(session=Session())
    models = {
        "Plan": PlanModel,
        "SystemSnapshot": SnapshotModel,
        "Generator": GeneratorModel,
        "Battery": BatteryModel,
        "Forecast": ForecastModel,
        "ReserveAssessment": ReserveAssessmentModel,
        "AgentRunTrace": AgentRunTraceModel,
        "ToolCallLog": ToolCallLogModel,
    }

    from contextlib import contextmanager
    @contextmanager
    def fake_tool_call(*args, **kwargs):
        yield 501, SimpleNamespace()

    monkeypatch.setattr(pt, "tool_call", fake_tool_call)
    monkeypatch.setattr(pt, "finish_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(pt, "load_decision_policy", lambda version: {
        "status": "APPROVED",
        "interval_minutes": 15,
        "cost_model": {},
    })

    seen_series = []
    def fake_build(snapshot, generators, batteries, series, strategy, policy,
                   horizon_minutes, objectives, reserve_required=None):
        seen_series.append([dict(x) for x in series])
        intervals = [
            pt.DispatchInterval(
                index=i,
                start=NOW + timedelta(minutes=i * 15),
                generators={"1": pt.GeneratorDispatchPoint(output_mw=float(point["demand"]))},
                batteries={},
                solar_curtailment_mw=0.0,
                residual_imbalance_mw=0.0,
            )
            for i, point in enumerate(series)
        ]
        return {
            "dispatch": pt.PlanDispatch(interval_minutes=15, start_time=NOW, intervals=intervals),
            "optimization_status": "FEASIBLE",
            "solver_status": "OPTIMAL",
            "objective": 0.0,
            "reserve_shortfall": 0.0,
            "slack_diagnostics": [],
        }, None

    monkeypatch.setattr(pt, "_build_candidate", fake_build)

    persisted = []
    def fake_persist(db, Plan, snapshot, request, candidate, run_id):
        persisted.append(candidate.candidate_id)
        candidate.plan_id = 99
        return SimpleNamespace(id=99)
    monkeypatch.setattr(pt, "_persist_candidate", fake_persist)

    legacy = pt.generate_and_optimize_plans(
        {
            "snapshot_id": 7,
            "horizon_minutes": 30,
            "required_candidate_count": 1,
            "strategies": ["SOLAR_ONLY"],
        },
        db=db,
        models=models,
    )
    assert [p["demand"] for p in seen_series[-1]] == [200.0, 50.0]
    assert legacy["plans"][0]["plan_id"] == 99
    assert len(persisted) == 1

    dp_result = pt.generate_and_optimize_plans(
        {
            "snapshot_id": 7,
            "horizon_minutes": 30,
            "required_candidate_count": 1,
            "strategies": ["SOLAR_ONLY"],
            "dynamic_pricing_context": {"tool_call_id": 41},
        },
        db=db,
        models=models,
    )
    assert [p["demand"] for p in seen_series[-1]] == [180.0, 70.0]
    assert dp_result["plans"][0]["plan_id"] == 99
    assert dp_result["plans"][0]["dispatch"]["dynamic_pricing_context"]["tool_call_id"] == 41
    assert len(persisted) == 2  # Phase 5 persists DP-assisted Plans for validation/evaluation
    metadata = dp_result["plans"][0]["objective_metrics"]["dynamic_pricing_context"]
    assert metadata["tool_call_id"] == 41
    assert metadata["base_demand_mw"] == [200.0, 50.0]
    assert metadata["effective_demand_mw"] == [180.0, 70.0]


def test_resolver_rejects_output_provenance_id_that_does_not_match_log_row():
    log = make_log(ident=41, output=dp_output(tool_call_id=999))
    db = FakeDB(ToolCallLog, log)
    with pytest.raises(ValueError, match="output provenance id does not match"):
        pt._resolve_dynamic_pricing_planning_context(
            request(41), db=db, ToolCallLog=ToolCallLog,
            snapshot=SimpleNamespace(id=7, timestamp=NOW), interval_minutes=15
        )
