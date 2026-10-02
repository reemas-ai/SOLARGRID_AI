from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
import importlib.util
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import load_dynamic_pricing_config
from schemas import (
    DomainStatus,
    DynamicPricingOutcomeMetrics,
    DynamicPricingRequest,
    OutcomeDiagnosisResult,
)
from tools.dynamic_pricing_tools import (
    DynamicPricingIntervalContext,
    analyze_dynamic_pricing_core,
    evaluate_dynamic_pricing_outcome,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _contexts():
    return [
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


def _result():
    result = analyze_dynamic_pricing_core(
        DynamicPricingRequest(snapshot_id=7, horizon_minutes=30, config_version="v1.0-demo"),
        _contexts(),
    )
    return result.model_copy(update={"tool_call_id": 41})


def _actual_actions(response_factor: float = 1.0, *, include_solar: bool = True):
    result = _result()
    requested = float(result.total_shifted_mw)
    actual = requested * response_factor
    rows = []
    for analysis in result.hourly_analysis:
        delta = 0.0
        if analysis.interval_index == 0:
            delta = -actual
        elif analysis.interval_index == 1:
            delta = actual
        generators = []
        if include_solar:
            generators = [{"id": "solar", "actual_mw": analysis.solar_generation_mw}]
        rows.append({
            "index": analysis.interval_index,
            "generators": generators,
            "batteries": [],
            "demand_before_dynamic_pricing_mw": analysis.demand_mw,
            "demand_after_dynamic_pricing_mw": analysis.demand_mw + delta,
        })
    return {
        "intervals": rows,
        "dynamic_pricing": {
            "tool_call_id": 41,
            "energy_scope": "SOLAR_ONLY",
            "response_factor": response_factor,
            "total_shifted_mw": actual,
            "conservation_verified": True,
        },
    }


def test_phase7_config_preserves_standalone_deviation_tolerance():
    cfg = load_dynamic_pricing_config("v1.0-demo")
    assert cfg["energy_scope"] == "SOLAR_ONLY"
    assert cfg["outcome_verification"]["deviation_tolerance_ratio"] == pytest.approx(0.15)


def test_phase7_expected_impact_uses_configured_export_when_interval_override_is_missing():
    contexts = _contexts()
    contexts = [
        DynamicPricingIntervalContext(**{**item.__dict__, "export_capacity_mw": None})
        for item in contexts
    ]
    result = analyze_dynamic_pricing_core(
        DynamicPricingRequest(snapshot_id=7, horizon_minutes=30, config_version="v1.0-demo"),
        contexts,
    )
    assert result.domain_status == DomainStatus.OK
    assert result.expected_impact.expected_load_shift_mw is not None
    assert result.expected_impact.expected_surplus_reduction_mw is not None


def test_phase7_tool17_populates_expected_impact_instead_of_none_placeholders():
    result = _result()
    impact = result.expected_impact
    assert result.total_shifted_mw > 0
    assert impact.expected_load_shift_mw == pytest.approx(result.total_shifted_mw)
    assert impact.expected_surplus_reduction_mw == pytest.approx(result.total_shifted_mw, abs=1e-3)
    assert impact.expected_curtailment_reduction_mw == pytest.approx(result.total_shifted_mw, abs=1e-3)
    assert impact.expected_utilization_improvement_ratio is not None
    assert impact.expected_utilization_improvement_ratio > 0


def test_phase7_exact_execution_matches_expected_impact():
    outcome = evaluate_dynamic_pricing_outcome(
        _result(), _actual_actions(1.0), deviation_tolerance_ratio=0.15
    )
    assert outcome.verification_status == "COMPLETED"
    assert outcome.goal_result == "ACHIEVED"
    assert outcome.significant_deviation is False
    assert outcome.actual_load_shift_mw == pytest.approx(outcome.expected_load_shift_mw)
    assert outcome.actual_surplus_reduction_mw == pytest.approx(outcome.expected_surplus_reduction_mw)
    assert outcome.actual_curtailment_reduction_mw == pytest.approx(outcome.expected_curtailment_reduction_mw)
    assert outcome.actual_utilization_improvement_ratio == pytest.approx(
        outcome.expected_utilization_improvement_ratio
    )
    assert all(value == pytest.approx(0.0) for value in outcome.deviations.values())
    assert outcome.lesson_signal is None


def test_phase7_partial_response_is_detected_and_generates_price_sensitivity_lesson_signal():
    outcome = evaluate_dynamic_pricing_outcome(
        _result(), _actual_actions(0.5), deviation_tolerance_ratio=0.15
    )
    assert outcome.verification_status == "COMPLETED"
    assert outcome.goal_result == "PARTIAL"
    assert outcome.significant_deviation is True
    assert outcome.deviations["load_shift_mw"] == pytest.approx(-0.5)
    assert outcome.deviations["surplus_reduction_mw"] == pytest.approx(-0.5)
    assert outcome.lesson_code == "FLEXIBLE_RESPONSE_OVER_ESTIMATED"
    assert "overestimated flexible-load response" in outcome.lesson_signal


def test_phase7_missing_actual_solar_preserves_unknown_but_keeps_observed_load_response_deviation():
    outcome = evaluate_dynamic_pricing_outcome(
        _result(), _actual_actions(0.5, include_solar=False), deviation_tolerance_ratio=0.15
    )
    assert outcome.verification_status == "PARTIAL_EVIDENCE"
    assert outcome.goal_result == "NOT_EVALUATED"
    assert outcome.actual_load_shift_mw is not None
    assert outcome.actual_surplus_reduction_mw is None
    assert outcome.actual_curtailment_reduction_mw is None
    assert outcome.actual_utilization_improvement_ratio is None
    assert outcome.significant_deviation is True
    assert outcome.lesson_code == "FLEXIBLE_RESPONSE_OVER_ESTIMATED"


def test_phase7_public_tool14_schema_exposes_dynamic_pricing_outcome():
    dp = DynamicPricingOutcomeMetrics(
        tool_call_id=41,
        verification_status="COMPLETED",
        goal_result="ACHIEVED",
        expected_load_shift_mw=10.0,
        actual_load_shift_mw=10.0,
        significant_deviation=False,
    )
    model = OutcomeDiagnosisResult(execution_id=5, dynamic_pricing_outcome=dp)
    assert model.dynamic_pricing_outcome.tool_call_id == 41
    assert model.dynamic_pricing_outcome.goal_result == "ACHIEVED"


def test_phase7_tool14_persists_dp_expected_actual_inside_existing_operational_outcome():
    source = (ROOT / "tools" / "operational_tools.py").read_text(encoding="utf-8")
    assert "_dynamic_pricing_outcome_for_tool14" in source
    assert '"dynamic_pricing": dynamic_pricing_outcome' in source
    assert '"dynamic_pricing_outcome": dynamic_pricing_outcome' in source
    assert '"DYNAMIC_PRICING_IMPACT_DEVIATION"' in source
    # No Dynamic-Pricing-specific outcome table should be introduced.
    db_source = (ROOT / "database.py").read_text(encoding="utf-8")
    assert "class DynamicPricingOutcome" not in db_source


def test_phase7_memory_creates_specific_dp_lesson_from_material_deviation(monkeypatch):
    # Load agents/memory.py against a tiny in-memory database contract so the
    # actual SystemMemoryManager method is exercised without Flask-SQLAlchemy.
    lessons = []

    class FakeQuery:
        def __init__(self, rows): self.rows = rows
        def all(self): return list(self.rows)

    class LessonMemory:
        query = FakeQuery(lessons)
        def __init__(self, **kwargs):
            self.id = None
            for k, v in kwargs.items(): setattr(self, k, v)

    class OperationalOutcome: pass
    class PlanExecution: pass
    class Plan: pass
    class AgentRunTrace: pass
    class ToolCallLog: pass
    class SystemSnapshot: pass
    class ImbalanceEvent: pass
    class ForecastErrorLog: pass

    plan = SimpleNamespace(strategy_label="COST_MIN", plan_name="COST_MIN", status="EXECUTED")
    execution = SimpleNamespace(id=22, status="SUCCESS", plan=plan)
    actual = {
        "goal_result": "ACHIEVED",
        "goal_criteria": {"balance": True, "reserve": True},
        "dynamic_pricing": {
            "significant_deviation": True,
            "lesson_code": "FLEXIBLE_RESPONSE_OVER_ESTIMATED",
            "lesson_signal": "The assumed price sensitivity overestimated flexible-load response.",
            "expected_load_shift_mw": 20.0,
            "actual_load_shift_mw": 10.0,
            "expected_surplus_reduction_mw": 20.0,
            "actual_surplus_reduction_mw": 10.0,
            "deviations": {"load_shift_mw": -0.5},
        },
    }
    outcome = SimpleNamespace(
        id=5,
        execution=execution,
        actual_outcome=json.dumps(actual),
        operational_impact=json.dumps([]),
    )

    class Session:
        def get(self, model, ident):
            if model is OperationalOutcome and ident == 5: return outcome
            return None
        def add(self, obj):
            if isinstance(obj, LessonMemory):
                obj.id = len(lessons) + 1
                lessons.append(obj)
        def commit(self): pass
        def rollback(self): pass

    fake_db = SimpleNamespace(session=Session(), or_=lambda *args: args)
    fake_mod = ModuleType("database")
    for name, value in {
        "db": fake_db,
        "AgentRunTrace": AgentRunTrace,
        "ToolCallLog": ToolCallLog,
        "SystemSnapshot": SystemSnapshot,
        "ImbalanceEvent": ImbalanceEvent,
        "Plan": Plan,
        "PlanExecution": PlanExecution,
        "OperationalOutcome": OperationalOutcome,
        "LessonMemory": LessonMemory,
        "ForecastErrorLog": ForecastErrorLog,
    }.items():
        setattr(fake_mod, name, value)

    monkeypatch.setitem(sys.modules, "database", fake_mod)
    spec = importlib.util.spec_from_file_location("phase7_memory_under_test", ROOT / "agents" / "memory.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    decision = module.SystemMemoryManager().evaluate_outcome_for_reusable_lesson(5)
    assert decision["status"] == "CREATED_OR_REINFORCED"
    assert decision["lesson_code"] == "FLEXIBLE_RESPONSE_OVER_ESTIMATED"
    assert len(lessons) == 1
    lesson = lessons[0]
    assert lesson.conditions["dynamic_pricing"] is True
    assert lesson.conditions["energy_scope"] == "SOLAR_ONLY"
    assert "expected_load_shift_mw=20.0" in lesson.evidence_summary
    assert "actual_load_shift_mw=10.0" in lesson.evidence_summary
    assert "overestimated flexible-load response" in lesson.planning_implication
