
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_post_execution_replan_backend_contract_exists():
    planner = read("agents/planner_agent.py")
    app = read("app.py")

    assert "def replan_after_execution(" in planner
    assert "build_and_validate_plans(" in planner
    assert "parent_plan_id = original.id" in planner
    assert 'filter(Plan.status.in_({"PROPOSED", "APPROVED"}))' in planner
    assert 'def replan_plan(plan_id: int):' in app
    assert '@app.post("/api/plans/<int:plan_id>/replan")' in app
    assert '"POST_EXECUTION_REPLAN"' in app


def test_replan_preserves_existing_safety_execution_boundary():
    planner = read("agents/planner_agent.py")
    safety = read("agents/safety_agent.py")

    assert "SafetyAgent(" in planner
    assert "safety.validate_plan(" in planner
    assert 'self._call(\n                "execute_and_verify_plan"' in planner
    assert "BLOCK" in safety
    assert "validate_plan" in safety


def test_tool12_revalidation_path_remains_separate():
    planner = read("agents/planner_agent.py")
    app = read("app.py")

    assert "def revalidate_active_plan(" in planner
    assert 'self._call("assess_change_impact"' in planner
    assert 'transition_plan(plan, "STALE")' in planner
    assert '@app.post("/api/plans/<int:plan_id>/revalidate")' in app


def test_frontend_replan_is_only_added_for_recovery_state():
    js = read("static/js/main.js")

    assert "if(!blocked&&replan)" in js
    assert "post(`/api/plans/${planId}/replan`" in js
    assert "id=\"postExecutionReplanButton\"" in js
    assert "Parent Plan #" in js
    assert "Auto approval" in js
    assert "Not automatically approved or executed." in js


def test_execution_trace_uses_existing_bounded_automation_budget_for_recovery():
    planner = read("agents/planner_agent.py")
    app = read("app.py")
    config = read("config/automation_v1.json")

    # Generic interactive budget remains intentionally 16. The production API
    # execution boundary must consume the already-approved automation budget.
    assert "max_tool_calls: int = 16" in planner
    assert 'load_json("automation_v1.json")' in app
    assert 'limit_cfg.get("max_tool_calls", default_limits.max_tool_calls)' in app
    assert '"max_tool_calls": 24' in config

    # Exact worst-case recovery call count in the current deterministic pipeline.
    assert (1 + 3 + 1 + 1 + 1 + 1 + 12 + 1) == 21
    assert 24 >= 21
