from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace
import importlib.util
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_planner_module(monkeypatch):
    fake_dispatcher = ModuleType("agents.tool_dispatcher")
    fake_dispatcher.ToolContext = SimpleNamespace
    fake_dispatcher.REQUEST_SCHEMAS = {}
    fake_dispatcher.execute_tool = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "agents.tool_dispatcher", fake_dispatcher)

    fake_db = ModuleType("database")
    fake_db.Plan = type("Plan", (), {})
    fake_db.SystemSnapshot = type("SystemSnapshot", (), {})
    fake_db.ReserveAssessment = type("ReserveAssessment", (), {})
    monkeypatch.setitem(sys.modules, "database", fake_db)

    fake_safety = ModuleType("agents.safety_agent")
    fake_safety.SafetyAgent = type("SafetyAgent", (), {})
    fake_safety.SafetyDecision = SimpleNamespace(ALLOW="ALLOW")
    monkeypatch.setitem(sys.modules, "agents.safety_agent", fake_safety)

    fake_memory = ModuleType("agents.memory")
    fake_memory.SystemMemoryManager = type("SystemMemoryManager", (), {})
    monkeypatch.setitem(sys.modules, "agents.memory", fake_memory)

    spec = importlib.util.spec_from_file_location(
        "phase8_planner_under_test", ROOT / "agents" / "planner_agent.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _planner(monkeypatch, *, action_required=True):
    module = _load_planner_module(monkeypatch)
    planner = module.PlannerAgent(context=SimpleNamespace(db=None, session=None, run_id=77))
    planner.resolve_validation_context = lambda **kwargs: {
        "snapshot_id": kwargs["snapshot_id"],
        "horizon_minutes": kwargs["horizon_minutes"],
        "reserve_assessment_id": 12,
        "network_id": "demo-grid",
        "decision_policy_version": kwargs["decision_policy_version"],
    }
    calls = []

    def fake_call(name, payload):
        calls.append((name, payload))
        if name == "analyze_dynamic_pricing":
            return {
                "tool_call_id": 41,
                "snapshot_id": 7,
                "horizon_minutes": 30,
                "energy_scope": "SOLAR_ONLY",
                "action_required": action_required,
                "domain_status": "OK",
                "total_shifted_mw": 20.0 if action_required else 0.0,
            }
        if name == "generate_and_optimize_plans":
            return {
                "plans": [
                    {"plan_id": 101, "candidate_id": "A"},
                    {"plan_id": 102, "candidate_id": "B"},
                ]
            }
        if name in {"check_generator_constraints", "check_battery_constraints", "run_power_flow"}:
            return {"status": "PASS", "plan_id": payload["plan_id"]}
        if name == "evaluate_plans":
            return {"selected_plan_id": 101, "evaluations": [{"plan_id": 101, "is_feasible": True}]}
        raise AssertionError(f"Unexpected tool call: {name}")

    planner._call = fake_call
    return planner, calls


def test_phase8_planner_orchestrates_tool17_into_existing_05_09_lifecycle(monkeypatch):
    planner, calls = _planner(monkeypatch, action_required=True)
    result = planner.build_and_validate_dynamic_pricing_plans(
        snapshot_id=7,
        horizon_minutes=30,
        required_candidate_count=2,
    )

    assert result["status"] == "PLANS_EVALUATED"
    assert result["dynamic_pricing_tool_call_id"] == 41
    assert result["selected_plan_id"] == 101
    names = [name for name, _ in calls]
    assert names == [
        "analyze_dynamic_pricing",
        "generate_and_optimize_plans",
        "check_generator_constraints",
        "check_battery_constraints",
        "run_power_flow",
        "check_generator_constraints",
        "check_battery_constraints",
        "run_power_flow",
        "evaluate_plans",
    ]
    generate_payload = next(payload for name, payload in calls if name == "generate_and_optimize_plans")
    assert generate_payload["dynamic_pricing_context"] == {"tool_call_id": 41}
    # Critical trust boundary: no caller/LLM-supplied Dynamic Pricing MW/price values.
    assert set(generate_payload["dynamic_pricing_context"]) == {"tool_call_id"}


def test_phase8_planner_skips_tool05_when_tool17_has_no_action(monkeypatch):
    planner, calls = _planner(monkeypatch, action_required=False)
    result = planner.build_and_validate_dynamic_pricing_plans(
        snapshot_id=7,
        horizon_minutes=30,
        required_candidate_count=2,
    )
    assert result["status"] == "NO_ACTION"
    assert result["planning"] is None
    assert [name for name, _ in calls] == ["analyze_dynamic_pricing"]


def test_phase8_legacy_planning_still_has_no_dynamic_pricing_context(monkeypatch):
    planner, calls = _planner(monkeypatch, action_required=True)
    result = planner.build_and_validate_plans(
        snapshot_id=7,
        horizon_minutes=30,
        required_candidate_count=2,
    )
    assert result["selected_plan_id"] == 101
    generate_payload = next(payload for name, payload in calls if name == "generate_and_optimize_plans")
    assert "dynamic_pricing_context" not in generate_payload


def test_phase8_app_exposes_analysis_and_planning_entrypoints_without_parallel_execution_api():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert '@app.post("/api/dynamic-pricing/analyze")' in source
    assert '@app.post("/api/dynamic-pricing/plans")' in source
    assert "build_and_validate_dynamic_pricing_plans" in source
    # Dynamic Pricing plans deliberately reuse the existing Plan endpoints.
    assert "/api/dynamic-pricing/approve" not in source
    assert "/api/dynamic-pricing/execute" not in source
    assert '@app.post("/api/plans/<int:plan_id>/approve")' in source
    assert '@app.post("/api/plans/<int:plan_id>/execute")' in source


def test_phase8_ui_exposes_tool17_and_reuses_existing_plan_lifecycle():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "js" / "main.js").read_text(encoding="utf-8")
    assert 'id="analyzeDynamicPricing"' in html
    assert 'id="generateDynamicPricingPlan"' in html
    assert "/api/dynamic-pricing/analyze" in js
    assert "/api/dynamic-pricing/plans" in js
    assert "analyze_dynamic_pricing:{number:'17'" in js
    assert "planAction(p.id,'approve')" in js
    assert "planAction(p.id,'execute')" in js
    assert "dynamic_pricing_outcome" in js


def test_phase8_current_integration_remains_solar_only_and_contains_no_wind_contract():
    schema = (ROOT / "schemas.py").read_text(encoding="utf-8")
    tool = (ROOT / "tools" / "dynamic_pricing_tools.py").read_text(encoding="utf-8")
    config = (ROOT / "config" / "dynamic_pricing_v1.json").read_text(encoding="utf-8")
    assert "wind_generation_mw" not in schema
    assert "wind_generation_mw" not in tool
    assert '"energy_scope": "SOLAR_ONLY"' in config


def test_phase8_planner_prompt_no_longer_describes_tool17_as_phase3_analysis_only():
    prompt = (ROOT / "agents" / "prompts.py").read_text(encoding="utf-8")
    assert "In the current Phase 3 architecture" not in prompt
    assert "pass only its persisted" in prompt
    assert "same\nTools 06-09, human approval, Safety, Tool 13, Tool 14, and Memory lifecycle" in prompt


def test_phase8_cross_component_tool17_execution_outcome_to_lesson_memory(monkeypatch):
    """One cross-component regression: Tool17 -> simulation/Tool13 semantics -> Tool14 DP outcome -> LessonMemory."""
    from datetime import datetime, timedelta, timezone
    import json

    from schemas import DynamicPricingRequest
    from tools.dynamic_pricing_tools import (
        DynamicPricingIntervalContext,
        analyze_dynamic_pricing_core,
        evaluate_dynamic_pricing_outcome,
    )
    import simulation

    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    contexts = [
        DynamicPricingIntervalContext(
            interval_index=0, target_time=now + timedelta(minutes=15),
            solar_generation_mw=20.0, demand_mw=200.0, other_generation_mw=40.0,
            battery_charging_headroom_mw=0.0, flexible_load_mw=100.0,
            max_shiftable_load_mw=100.0, export_capacity_mw=0.0,
            baseline_price_per_mwh=60.0,
        ),
        DynamicPricingIntervalContext(
            interval_index=1, target_time=now + timedelta(minutes=30),
            solar_generation_mw=260.0, demand_mw=100.0, other_generation_mw=40.0,
            battery_charging_headroom_mw=10.0, flexible_load_mw=20.0,
            max_shiftable_load_mw=20.0, export_capacity_mw=10.0,
            baseline_price_per_mwh=60.0,
        ),
    ]
    tool17 = analyze_dynamic_pricing_core(
        DynamicPricingRequest(snapshot_id=7, horizon_minutes=30, config_version="v1.0-demo"),
        contexts,
    ).model_copy(update={"tool_call_id": 41})
    assert tool17.action_required is True

    # Reuse the Phase-6 fake persistence contract but replace its Tool17 log with
    # the actual deterministic Tool17 result constructed above.
    phase6_name = "phase8_phase6_helper"
    spec6 = importlib.util.spec_from_file_location(phase6_name, ROOT / "tests" / "test_dynamic_pricing_phase6_execution.py")
    phase6 = importlib.util.module_from_spec(spec6)
    assert spec6.loader is not None
    monkeypatch.setitem(sys.modules, phase6_name, phase6)
    spec6.loader.exec_module(phase6)
    _plan, _snapshot, log = phase6.install_fake_database(monkeypatch)
    log.output_json = tool17.model_dump(mode="json")

    execution = simulation.apply_plan(99, simulation_options={"dynamic_pricing_response_factor": 0.5})
    assert execution["execution_status"] == "PARTIAL_FAIL"
    assert execution["actual_actions"]["dynamic_pricing"]["conservation_verified"] is True

    dp_outcome = evaluate_dynamic_pricing_outcome(
        tool17,
        execution["actual_actions"],
        deviation_tolerance_ratio=0.15,
    )
    assert dp_outcome.significant_deviation is True
    assert dp_outcome.lesson_code == "FLEXIBLE_RESPONSE_OVER_ESTIMATED"

    lessons = []

    class FakeQuery:
        def __init__(self, rows): self.rows = rows
        def all(self): return list(self.rows)

    class LessonMemory:
        query = FakeQuery(lessons)
        def __init__(self, **kwargs):
            self.id = None
            for key, value in kwargs.items(): setattr(self, key, value)

    class OperationalOutcome: pass
    class PlanExecution: pass
    class Plan: pass
    class AgentRunTrace: pass
    class ToolCallLog: pass
    class SystemSnapshot: pass
    class ImbalanceEvent: pass
    class ForecastErrorLog: pass

    plan = SimpleNamespace(strategy_label="COST_MIN", plan_name="COST_MIN", status="EXECUTED")
    execution_row = SimpleNamespace(id=22, status=execution["execution_status"], plan=plan)
    outcome_payload = {
        "goal_result": "ACHIEVED",
        "goal_criteria": {"balance": True, "reserve": True},
        "dynamic_pricing": dp_outcome.model_dump(mode="json"),
    }
    outcome_row = SimpleNamespace(
        id=5,
        execution=execution_row,
        actual_outcome=json.dumps(outcome_payload),
        operational_impact=json.dumps([]),
    )

    class Session:
        def get(self, model, ident):
            if model is OperationalOutcome and ident == 5: return outcome_row
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

    memory_name = "phase8_memory_under_test"
    specm = importlib.util.spec_from_file_location(memory_name, ROOT / "agents" / "memory.py")
    memory_module = importlib.util.module_from_spec(specm)
    assert specm.loader is not None
    monkeypatch.setitem(sys.modules, memory_name, memory_module)
    specm.loader.exec_module(memory_module)

    decision = memory_module.SystemMemoryManager().evaluate_outcome_for_reusable_lesson(5)
    assert decision["status"] == "CREATED_OR_REINFORCED"
    assert decision["lesson_code"] == "FLEXIBLE_RESPONSE_OVER_ESTIMATED"
    assert len(lessons) == 1
    assert lessons[0].conditions["dynamic_pricing"] is True
    assert lessons[0].conditions["energy_scope"] == "SOLAR_ONLY"


def test_phase8_optional_demo_dataset_is_additive_and_solar_only():
    import json
    data = json.loads((ROOT / "datasets" / "dynamic_pricing.json").read_text(encoding="utf-8"))
    assert data["scenario"] == "dynamic_pricing"
    assert data["snapshot"]["other_gen_mw"] == 0.0
    assert any(row.get("is_flexible") is True for row in data["loads"])
    assert all("wind" not in json.dumps(row).lower() for row in data["forecasts"])
    assert (ROOT / "datasets" / "normal.json").exists()
    assert (ROOT / "datasets" / "problem.json").exists()
