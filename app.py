"""SolarGrid Flask application / frontend API boundary.

This module intentionally contains no grid calculations or safety policy.
All domain work is delegated to the existing PlannerAgent -> ToolDispatcher ->
Tools 01-16 stack so the web UI cannot bypass the backend architecture.
"""
from __future__ import annotations

import os
import json
import threading
from datetime import date, datetime, timezone
from functools import wraps
from typing import Any, Callable

from env_config import ENV_FILE, load_project_env, secret_is_configured
from flask import Flask, current_app, jsonify, render_template, request
from flask_cors import CORS
from sqlalchemy.inspection import inspect as sa_inspect

# Load the project-local .env before importing application modules that read environment settings.
# The path is resolved relative to this source tree, not the shell working directory.
load_project_env()

from database import (  # noqa: E402
    AgentRunTrace,
    HumanApproval,
    GridBus,
    Generator,
    Forecast,
    ImbalanceEvent,
    LessonMemory,
    OperationalOutcome,
    Plan,
    PlanEvaluation,
    PlanExecution,
    ReserveAssessment,
    SystemSnapshot,
    ToolCallLog,
    RAGEvidence,
    ExternalDataProvenance,
    OperationalEvent,
    MonitoringTick,
    db,
    init_db,
)
from agents.memory import SystemMemoryManager  # noqa: E402
from agents.planner_agent import PlannerAgent, PlannerLimits  # noqa: E402
from agents.tool_dispatcher import ToolContext  # noqa: E402
from plan_lifecycle import transition_plan  # noqa: E402
from map_status import status_from_availability  # noqa: E402
from solar_projects import SOLAR_PROJECTS, SOLAR_PROJECTS_BY_KEY, SOLAR_PROJECTS_BY_NAME  # noqa: E402
from external_data.service import ExternalDataService  # noqa: E402
from automation.monitor_service import get_monitor_service  # noqa: E402
from config_loader import load_json  # noqa: E402


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    """Application factory used by local development, tests and production."""
    app = Flask(__name__, template_folder="templates", static_folder="static")

    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-only-change-me"),
        SQLALCHEMY_DATABASE_URI=os.getenv(
            "SQLALCHEMY_DATABASE_URI", "sqlite:///solargrid.db"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=_env_bool(
            "SQLALCHEMY_TRACK_MODIFICATIONS", False
        ),
        SQLALCHEMY_ECHO=_env_bool("SQLALCHEMY_ECHO", False),
        JSON_SORT_KEYS=False,
    )
    if test_config:
        app.config.update(test_config)

    init_db(app)
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    with app.app_context():
        _repair_stale_standalone_runs()

    # Do not block the HTTP listener while Tool 16 warms its retrieval index.
    # The old synchronous startup path could leave an already-open browser tab
    # polling 127.0.0.1 while Flask was still importing/loading the embedding
    # backend, which surfaces in fetch() as the unhelpful browser error
    # "Failed to fetch".  Warm RAG in a daemon thread instead: the web/API
    # server becomes reachable immediately and execution can still use Tool 16.
    if _env_bool("SOLARGRID_RAG_PREWARM", True):
        app.config["SOLARGRID_RAG_PREWARM_STATUS"] = "WARMING"
        _start_rag_prewarm(app)
    else:
        app.config["SOLARGRID_RAG_PREWARM_STATUS"] = "DISABLED"

    @app.after_request
    def _api_no_store(response):
        # Prevent a stale browser tab from reusing old JSON or module responses
        # after the user switches normal/problem datasets or replaces a build.
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

    # --------------------------- Frontend shell ---------------------------
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        # Report configuration presence only; never expose secret values.
        return jsonify({
            "ok": True,
            "service": "solargrid-api",
            "build": "4.0-dynamic-pricing-integrated",
            "rag_prewarm": current_app.config.get("SOLARGRID_RAG_PREWARM_STATUS", "NOT_REQUESTED"),
            "environment": {
                "project_env_file_found": ENV_FILE.exists(),
                "project_env_file": ENV_FILE.name,
                "groq_configured": secret_is_configured("GROQ_API_KEY"),
                "eia_configured": secret_is_configured("EIA_API_KEY"),
                "eia_runtime_integration": "RESERVED_NOT_ACTIVE",
            },
        })

    # ---------------------- Grid map / site metadata ----------------------
    @app.get("/api/grid/sites")
    @api_errors
    def grid_sites():
        """Compatibility endpoint for the UI.

        The current project schema does not persist geographic coordinates on
        GridBus, so this endpoint reports buses as unmapped instead of assuming
        database columns that do not exist.
        """
        return jsonify({"sites": [], "unmapped_count": GridBus.query.count()})

    @app.get("/api/grid/plants")
    @api_errors
    def generation_plants():
        """Return UI-safe solar-plant markers derived from existing generators.

        The backend Generator model intentionally remains unchanged. Display
        metadata (solar location/coordinates) lives only in this web boundary
        and operational values still come from the active dataset.
        """
        return jsonify({"plants": _display_generation_plants()})

    @app.get("/api/dashboard/summary")
    @api_errors
    def dashboard_summary():
        """Compact backend-owned summary for the operator overview page."""
        snapshot = SystemSnapshot.query.order_by(SystemSnapshot.timestamp.desc()).first()
        latest_event = ImbalanceEvent.query.order_by(ImbalanceEvent.id.desc()).first()
        latest_run = AgentRunTrace.query.order_by(AgentRunTrace.id.desc()).first()
        latest_lesson = LessonMemory.query.order_by(LessonMemory.id.desc()).first()
        latest_operational_event = OperationalEvent.query.order_by(OperationalEvent.id.desc()).first()
        external_rows = ExternalDataService().status()
        reserve = None
        forecasts = []
        if snapshot is not None:
            reserve = (
                ReserveAssessment.query.filter_by(snapshot_id=snapshot.id)
                .order_by(ReserveAssessment.id.desc()).first()
            )
            forecasts = (
                Forecast.query.filter_by(snapshot_id=snapshot.id)
                .filter(Forecast.unit == "MW")
                .order_by(Forecast.target_time.asc(), Forecast.id.asc())
                .all()
            )
        return jsonify({
            "snapshot": _model_dict(snapshot) if snapshot else None,
            "reserve": _model_dict(reserve) if reserve else None,
            "latest_event": _model_dict(latest_event) if latest_event else None,
            "latest_run": _model_dict(latest_run) if latest_run else None,
            "latest_lesson": _model_dict(latest_lesson) if latest_lesson else None,
            "latest_operational_event": _model_dict(latest_operational_event) if latest_operational_event else None,
            "external_sources": external_rows,
            "data_trust": {"grid_telemetry": "SYNTHETIC_DEMO", "external_context": "REAL_OPEN_API_WHEN_FRESH_OR_CACHED"},
            "forecasts": [_model_dict(row) for row in forecasts],
            "plants": _display_generation_plants(),
        })

    @app.get("/api/memory/lessons")
    @api_errors
    def memory_lessons():
        limit = min(_positive_int(request.args.get("limit", 50), "limit"), 200)
        lessons = LessonMemory.query.order_by(LessonMemory.id.desc()).limit(limit).all()
        rows = []
        for item in lessons:
            row = _model_dict(item)
            source_outcome = db.session.get(OperationalOutcome, item.source_outcome_id) if item.source_outcome_id else None
            source_execution = source_outcome.execution if source_outcome else None
            row["last_confirmed_at"] = source_execution.execution_timestamp.isoformat() + "Z" if source_execution and source_execution.execution_timestamp else None
            row["source_execution_id"] = source_execution.id if source_execution else None
            row["confidence_percent"] = round(float(item.confidence_score) * 100) if item.confidence_score is not None else None
            row["eligibility_reason"] = _lesson_eligibility_reason(item, source_outcome)
            rows.append(row)
        return jsonify({"lessons": rows})

    @app.get("/api/memory/history")
    @api_errors
    def memory_history():
        """Return execution/outcome history supported by the current schema."""
        limit = min(_positive_int(request.args.get("limit", 30), "limit"), 100)
        executions = (
            PlanExecution.query
            .order_by(PlanExecution.id.desc())
            .limit(limit)
            .all()
        )
        rows = []
        for execution in executions:
            plan = execution.plan
            outcome = execution.outcome
            operator_summary = None
            if outcome is not None:
                try:
                    actual = json.loads(outcome.actual_outcome or "{}")
                except (TypeError, ValueError):
                    actual = {}
                observed = actual.get("observed") if isinstance(actual.get("observed"), dict) else {}
                operator_summary = {
                    "execution_result": execution.status,
                    "goal_result": actual.get("goal_result", "NOT_EVALUATED"),
                    "grid_balance": actual.get("goal_criteria", {}).get("balance") if isinstance(actual.get("goal_criteria"), dict) else None,
                    "reserve": actual.get("goal_criteria", {}).get("reserve") if isinstance(actual.get("goal_criteria"), dict) else None,
                    "reserve_required_mw": observed.get("required_reserve_mw"),
                    "reserve_actual_mw": observed.get("actual_reserve_mw"),
                    "reserve_margin_mw": observed.get("reserve_margin_mw"),
                    "reserve_status": observed.get("reserve_status"),
                    "replan_required": actual.get("goal_result") in {"NOT_ACHIEVED", "PARTIAL"} or execution.status in {"FAILED", "PARTIAL_FAIL"},
                    "dynamic_pricing": actual.get("dynamic_pricing") if isinstance(actual.get("dynamic_pricing"), dict) else None,
                }
            rows.append({
                "attempt": None,
                "execution": _model_dict(execution),
                "plan": {
                    "id": plan.id,
                    "plan_name": plan.plan_name,
                    "strategy_label": plan.strategy_label,
                    "status": plan.status,
                } if plan else None,
                "outcome": _model_dict(outcome) if outcome else None,
                "operator_summary": operator_summary,
            })
        return jsonify({"history": rows})

    # ------------------------- State: Tools 01-04 -------------------------
    @app.post("/api/state/assess")
    @api_errors
    def assess_state():
        body = _json_body()
        horizon = _positive_int(body.get("horizon_minutes", 120), "horizon_minutes")
        include_external = str(body.get("include_external", "true")).strip().lower() in {"1", "true", "yes", "on"}
        return _run_with_trace(
            "STATE_ASSESSMENT",
            lambda planner: _assess_with_external_context(
                planner, horizon_minutes=horizon, include_external=include_external
            ),
        )

    @app.get("/api/state/latest")
    @api_errors
    def latest_state():
        snapshot = SystemSnapshot.query.order_by(SystemSnapshot.timestamp.desc()).first()
        if snapshot is None:
            return jsonify({"snapshot": None})
        return jsonify({"snapshot": _model_dict(snapshot)})

    @app.get("/api/state/snapshots/<int:snapshot_id>")
    @api_errors
    def get_snapshot(snapshot_id: int):
        snapshot = db.session.get(SystemSnapshot, snapshot_id)
        if snapshot is None:
            return _error("SystemSnapshot not found", 404)
        return jsonify({"snapshot": _model_dict(snapshot)})

    # ------------------------- Full LLM agent loop ------------------------
    @app.post("/api/agent/run")
    @api_errors
    def run_agent():
        body = _json_body()
        message = str(body.get("message", "")).strip()
        if not message:
            return _error("'message' is required", 400)

        snapshot_id = body.get("current_snapshot_id")
        if snapshot_id is not None:
            snapshot_id = _positive_int(snapshot_id, "current_snapshot_id")

        # Import lazily so .env has already been loaded before llm.py builds its client.
        from llm import call_llm

        return _run_with_trace(
            "USER_PROMPT",
            lambda planner: {
                "answer": planner.run_llm_tool_loop(
                    message,
                    llm=call_llm,
                    current_snapshot_id=snapshot_id,
                )
            },
        )

    # ----------------------- Dynamic Pricing: Tool 17 -----------------------
    @app.post("/api/dynamic-pricing/analyze")
    @api_errors
    def analyze_dynamic_pricing_api():
        body = _json_body()
        snapshot_id = _required_positive_int(body, "snapshot_id")
        horizon = _positive_int(body.get("horizon_minutes", 120), "horizon_minutes")
        config_version = str(body.get("config_version") or "v1.0-demo")
        return _run_with_trace(
            "DYNAMIC_PRICING_ANALYSIS",
            lambda planner: planner.analyze_dynamic_pricing(
                snapshot_id=snapshot_id,
                horizon_minutes=horizon,
                config_version=config_version,
            ),
        )

    @app.post("/api/dynamic-pricing/plans")
    @api_errors
    def generate_dynamic_pricing_plans():
        """Tool 17 -> existing Tools 05-09; resulting Plans use the normal lifecycle."""
        body = _json_body()
        snapshot_id = _required_positive_int(body, "snapshot_id")
        horizon = _positive_int(body.get("horizon_minutes", 120), "horizon_minutes")
        candidate_count = _positive_int(body.get("required_candidate_count", 4), "required_candidate_count")
        return _run_with_trace(
            "DYNAMIC_PRICING_PLAN_GENERATION",
            lambda planner: planner.build_and_validate_dynamic_pricing_plans(
                snapshot_id=snapshot_id,
                horizon_minutes=horizon,
                risk_event_id=body.get("risk_event_id"),
                reserve_assessment_id=body.get("reserve_assessment_id"),
                objectives=body.get("objectives") or [],
                decision_policy_version=body.get("decision_policy_version", "v1.0-demo"),
                dynamic_pricing_config_version=body.get("config_version", "v1.0-demo"),
                required_candidate_count=candidate_count,
                strategies=body.get("strategies"),
            ),
        )

    # ------------------------- Plans: Tools 05-09 -------------------------
    @app.post("/api/plans/generate")
    @api_errors
    def generate_plans():
        body = _json_body()
        snapshot_id = _required_positive_int(body, "snapshot_id")
        horizon = _positive_int(body.get("horizon_minutes", 120), "horizon_minutes")
        candidate_count = _positive_int(
            body.get("required_candidate_count", 4), "required_candidate_count"
        )

        return _run_with_trace(
            "PLAN_GENERATION",
            lambda planner: planner.build_and_validate_plans(
                snapshot_id=snapshot_id,
                horizon_minutes=horizon,
                risk_event_id=body.get("risk_event_id"),
                reserve_assessment_id=body.get("reserve_assessment_id"),
                objectives=body.get("objectives") or [],
                decision_policy_version=body.get(
                    "decision_policy_version", "v1.0-demo"
                ),
                required_candidate_count=candidate_count,
                strategies=body.get("strategies"),
            ),
        )

    @app.get("/api/plans")
    @api_errors
    def list_plans():
        limit = min(_positive_int(request.args.get("limit", 50), "limit"), 200)
        include_history = str(request.args.get("include_history", "false")).strip().lower() in {"1", "true", "yes", "on"}
        query = Plan.query
        current_snapshot = SystemSnapshot.query.order_by(SystemSnapshot.timestamp.desc(), SystemSnapshot.id.desc()).first()
        latest_batch_run_id = None
        archive_count = 0
        if not include_history and current_snapshot is not None:
            current_query = Plan.query.filter(Plan.snapshot_id == current_snapshot.id)
            latest_plan = current_query.order_by(Plan.id.desc()).first()
            if latest_plan is not None and latest_plan.run_id is not None:
                latest_batch_run_id = latest_plan.run_id
                query = current_query.filter(Plan.run_id == latest_batch_run_id)
                archive_count = max(0, Plan.query.count() - query.count())
            else:
                query = current_query
                archive_count = Plan.query.filter(Plan.snapshot_id != current_snapshot.id).count()
        plans = query.order_by(Plan.id.desc()).limit(limit).all()
        return jsonify({
            "plans": [_plan_dict(plan) for plan in plans],
            "current_snapshot_id": current_snapshot.id if current_snapshot else None,
            "plan_batch_run_id": latest_batch_run_id,
            "archive_count": archive_count,
        })

    @app.get("/api/plans/<int:plan_id>")
    @api_errors
    def get_plan(plan_id: int):
        plan = db.session.get(Plan, plan_id)
        if plan is None:
            return _error("Plan not found", 404)
        return jsonify({"plan": _plan_dict(plan, detailed=True)})

    @app.post("/api/plans/<int:plan_id>/validate")
    @api_errors
    def validate_plan(plan_id: int):
        """Retry deterministic validation for a generated candidate.

        This endpoint is intentionally separate from human approval. It may be
        used for PROPOSED candidates whose first Tool 09 pass failed or whose
        evidence is UNKNOWN. Approval remains impossible until a FEASIBLE
        evaluation exists for the plan's current snapshot.
        """
        plan = _get_plan_or_raise(plan_id)
        body = _json_body()
        snapshot_id = body.get("current_snapshot_id") or plan.snapshot_id
        snapshot_id = _positive_int(snapshot_id, "current_snapshot_id")
        return _run_with_trace(
            "PLAN_VALIDATION_RETRY",
            lambda planner: _validate_existing_plan(
                planner,
                plan_id=plan.id,
                context_snapshot_id=snapshot_id,
                decision_policy_version=body.get(
                    "decision_policy_version", "v1.0-demo"
                ),
            ),
        )

    @app.post("/api/plans/<int:plan_id>/approve")
    @api_errors
    def approve_plan(plan_id: int):
        plan = _get_plan_or_raise(plan_id)
        body = _json_body()
        if plan.status != "PROPOSED":
            return _error(f"Only PROPOSED plans can be approved (current: {plan.status})", 409)

        evaluation = (
            PlanEvaluation.query.filter_by(plan_id=plan.id)
            .order_by(PlanEvaluation.id.desc())
            .first()
        )
        if evaluation is None:
            return _error(
                "This plan has not completed deterministic validation. Generate a fresh plan or use Recheck Plan before approval.",
                409,
                code="PLAN_NOT_VALIDATED",
            )
        if evaluation.is_feasible is not True:
            reason = evaluation.rejection_reason or "The deterministic validation did not verify this plan as feasible."
            return _error(
                f"Plan cannot be approved: {reason}",
                409,
                code="PLAN_NOT_FEASIBLE",
            )

        approval = plan.approval or HumanApproval(plan_id=plan.id)
        approval.status = "APPROVED"
        approval.reviewer_comment = body.get("reviewer_comment")
        approval.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.add(approval)
        transition_plan(plan, "APPROVED")
        db.session.commit()
        return jsonify({"plan": _plan_dict(plan, detailed=True)})

    @app.post("/api/plans/<int:plan_id>/reject")
    @api_errors
    def reject_plan(plan_id: int):
        plan = _get_plan_or_raise(plan_id)
        body = _json_body()
        if plan.status != "PROPOSED":
            return _error(f"Only PROPOSED plans can be rejected (current: {plan.status})", 409)

        approval = plan.approval or HumanApproval(plan_id=plan.id)
        approval.status = "REJECTED"
        approval.reviewer_comment = body.get("reviewer_comment")
        approval.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.add(approval)
        transition_plan(plan, "REJECTED")
        db.session.commit()
        return jsonify({"plan": _plan_dict(plan, detailed=True)})

    @app.post("/api/plans/<int:plan_id>/execute")
    @api_errors
    def execute_plan(plan_id: int):
        plan = _get_plan_or_raise(plan_id)
        body = _json_body()
        snapshot_id = _required_positive_int(body, "current_snapshot_id")

        if plan.status != "APPROVED":
            return _error("Plan must be APPROVED before execution", 409)
        if plan.approval is None or plan.approval.status != "APPROVED":
            return _error("Approved HumanApproval record is required", 409)

        return _run_with_trace(
            "PLAN_EXECUTION",
            lambda planner: planner.execute_after_human_approval(
                plan_id=plan.id,
                approval_id=plan.approval.id,
                current_snapshot_id=snapshot_id,
            ),
        )

    # ------------------------ Monitoring: Tools 11-12 ---------------------
    @app.post("/api/plans/<int:plan_id>/monitor")
    @api_errors
    def monitor_plan(plan_id: int):
        _get_plan_or_raise(plan_id)
        body = _json_body()
        latest_snapshot = SystemSnapshot.query.order_by(SystemSnapshot.timestamp.desc(), SystemSnapshot.id.desc()).first()
        requested_snapshot_id = body.get("current_snapshot_id")
        snapshot_id = latest_snapshot.id if latest_snapshot is not None else _positive_int(requested_snapshot_id, "current_snapshot_id")
        return _run_with_trace(
            "PLAN_MONITORING",
            lambda planner: planner.monitor_active_plan(
                plan_id=plan_id, current_snapshot_id=snapshot_id
            ),
        )

    @app.post("/api/plans/<int:plan_id>/replan")
    @api_errors
    def replan_plan(plan_id: int):
        """Generate post-execution recovery candidates through the normal planner pipeline.

        This endpoint never approves or executes a replacement. Human Approval and
        the existing SafetyAgent-controlled execution path remain mandatory.
        """
        _get_plan_or_raise(plan_id)
        body = _json_body()
        candidate_count = _positive_int(body.get("required_candidate_count", 4), "required_candidate_count")
        if candidate_count > 4:
            raise ValueError("Post-execution replanning supports at most 4 configured candidate strategies")
        return _run_with_trace(
            "POST_EXECUTION_REPLAN",
            lambda planner: planner.replan_after_execution(
                plan_id=plan_id,
                required_candidate_count=candidate_count,
                decision_policy_version=str(body.get("decision_policy_version") or "v1.0-demo"),
            ),
        )

    @app.post("/api/plans/<int:plan_id>/revalidate")
    @api_errors
    def revalidate_plan(plan_id: int):
        _get_plan_or_raise(plan_id)
        body = _json_body()
        snapshot_id = _required_positive_int(body, "current_snapshot_id")
        return _run_with_trace(
            "PLAN_REVALIDATION",
            lambda planner: planner.revalidate_active_plan(
                plan_id=plan_id,
                current_snapshot_id=snapshot_id,
                monitoring_log_id=body.get("monitoring_log_id"),
            ),
        )

    # -------------------------- Outcome: Tool 14 --------------------------
    @app.post("/api/executions/<int:execution_id>/diagnose")
    @api_errors
    def diagnose_execution(execution_id: int):
        execution = db.session.get(PlanExecution, execution_id)
        if execution is None:
            return _error("PlanExecution not found", 404)
        body = _json_body()
        return _run_with_trace(
            "OUTCOME_DIAGNOSIS",
            lambda planner: planner.diagnose_execution(
                execution_id=execution_id,
                pre_execution_snapshot_id=body.get("pre_execution_snapshot_id"),
                post_execution_snapshot_id=body.get("post_execution_snapshot_id"),
                forecast_analysis_payload=body.get("forecast_analysis"),
            ),
        )

    @app.get("/api/monitor/status")
    @api_errors
    def autonomous_monitor_status():
        service = get_monitor_service(current_app._get_current_object())
        return jsonify({"monitor": service.status() if service else {"running": False, "mode": "unavailable"}})

    @app.post("/api/monitor/run-once")
    @api_errors
    def autonomous_monitor_run_once():
        # A manual tick exercises the exact same event-driven path as the scheduler.
        # It may assess/plan/validate, but the service always stops for Human Approval.
        service = get_monitor_service(current_app._get_current_object())
        if service is None:
            return _error("Autonomous monitor service is unavailable", 503)
        result = service.run_cycle(trigger_source="MANUAL_MONITOR_CYCLE")
        return jsonify({"ok": True, "message": "Monitor cycle completed", "result": _json_safe(result), "errors": []})

    @app.get("/api/external-data/status")
    @api_errors
    def external_data_status():
        service = ExternalDataService()
        return jsonify({
            "sources": service.status(),
            "trust_boundary": {
                "external_context": "REAL_OPEN_API_WHEN_FRESH_OR_CACHED",
                "grid_telemetry": "SYNTHETIC_DEMO",
                "irradiance_to_mw_conversion": False,
            },
        })

    @app.post("/api/external-data/refresh")
    @api_errors
    def external_data_refresh():
        body = _json_body()
        requested = str(body.get("source") or "ALL").upper()
        service = ExternalDataService()
        if requested == "ALL":
            rows = service.refresh_all(consumed_by="OPERATOR_REFRESH")
        else:
            rows = [service.refresh(requested, consumed_by="OPERATOR_REFRESH")]
        return jsonify({"ok": True, "sources": [service.serialize(row) for row in rows]})

    # -------------------------- Observability -----------------------------
    @app.get("/api/agent/runs")
    @api_errors
    def list_runs():
        limit = min(_positive_int(request.args.get("limit", 50), "limit"), 200)
        runs = AgentRunTrace.query.order_by(AgentRunTrace.id.desc()).limit(limit).all()
        return jsonify({"runs": [_run_dict(run) for run in runs]})

    @app.get("/api/agent/runs/<int:run_id>")
    @api_errors
    def get_run(run_id: int):
        run = db.session.get(AgentRunTrace, run_id)
        if run is None:
            return _error("AgentRunTrace not found", 404)
        calls = (
            ToolCallLog.query.filter_by(run_id=run_id)
            .order_by(ToolCallLog.step_index.asc(), ToolCallLog.id.asc())
            .all()
        )
        evidence = (
            RAGEvidence.query.filter_by(run_id=run_id)
            .order_by(RAGEvidence.retrieval_timestamp.asc(), RAGEvidence.id.asc())
            .all()
        )
        external = (
            ExternalDataProvenance.query.filter_by(consumed_run_id=run_id)
            .order_by(ExternalDataProvenance.id.asc()).all()
        )
        event = OperationalEvent.query.filter_by(run_id=run_id).order_by(OperationalEvent.id.desc()).first()
        return jsonify({
            "run": _run_dict(run),
            "tool_calls": [_model_dict(c) for c in calls],
            "engineering_evidence": [_rag_evidence_dict(row) for row in evidence],
            "external_data": [ExternalDataService.serialize(row) for row in external],
            "operational_event": _model_dict(event) if event else None,
        })

    @app.get("/api/rag/evidence")
    @api_errors
    def list_rag_evidence():
        limit = min(_positive_int(request.args.get("limit", 30), "limit"), 100)
        rows = RAGEvidence.query.order_by(RAGEvidence.id.desc()).limit(limit).all()
        return jsonify({"evidence": [_rag_evidence_dict(row) for row in rows]})

    # Start the scheduler only when explicitly enabled (or when running app.py directly).
    # Tests/imports remain deterministic; production can set SOLARGRID_AUTOMATION_ENABLED=true.
    default_automation = __name__ == "__main__"
    if not app.config.get("TESTING") and _env_bool("SOLARGRID_AUTOMATION_ENABLED", default_automation):
        monitor = get_monitor_service(app)
        if monitor is not None:
            monitor.start()
            app.extensions["solargrid_monitor"] = monitor

    return app


def _start_rag_prewarm(app: Flask) -> None:
    """Warm Tool 16 without delaying Flask socket availability.

    The thread is intentionally daemonized and never mutates the database.
    Any failure is recorded as status only; Tool 16 remains authoritative when
    it is actually invoked by the safety path.
    """
    def worker() -> None:
        try:
            from RAG.rag_engine import get_engine
            engine = get_engine()
            app.config["SOLARGRID_RAG_PREWARM_STATUS"] = f"READY:{engine.embedder.name}"
        except Exception as exc:  # optional acceleration must never kill HTTP startup
            app.config["SOLARGRID_RAG_PREWARM_STATUS"] = f"UNAVAILABLE:{type(exc).__name__}"
            try:
                app.logger.warning("Tool 16 background prewarm unavailable: %s", exc)
            except Exception:
                pass

    threading.Thread(
        target=worker,
        name="solargrid-rag-prewarm",
        daemon=True,
    ).start()


def _assess_with_external_context(planner: PlannerAgent, *, horizon_minutes: int, include_external: bool = True):
    """Refresh trusted public context, then run the unchanged Tools 01-04 assessment chain."""
    external_id = None
    service = None
    if include_external:
        service = ExternalDataService()
        record = service.refresh(
            "OPEN_METEO", consumed_by="TOOL_01_SYSTEM_STATE", run_id=planner.context.run_id
        )
        if str(record.status or "").upper() in {"FRESH", "CACHED"}:
            external_id = record.id
    result = planner.assess_current_state(
        horizon_minutes=horizon_minutes, external_weather_record_id=external_id
    )
    if service is not None and external_id is not None:
        snapshot_id = result.get("snapshot_id") if isinstance(result, dict) else None
        if snapshot_id is not None:
            service.mark_consumed(
                external_id, consumed_by="TOOL_01_SYSTEM_STATE",
                run_id=planner.context.run_id, snapshot_id=int(snapshot_id),
            )
    return result


def _run_dict(run: AgentRunTrace) -> dict[str, Any]:
    row = _model_dict(run)
    decisions = run.decisions if isinstance(run.decisions, dict) else {}
    row["summary"] = run.final_outcome_summary
    row["trigger_context"] = decisions.get("trigger_context") if isinstance(decisions.get("trigger_context"), dict) else None
    row["external_data_provenance_ids"] = decisions.get("external_data_provenance_ids") or []
    row["human_approval_required"] = decisions.get("human_approval_required")
    row["automatic_execution_allowed"] = decisions.get("automatic_execution_allowed")
    return row


def _rag_evidence_dict(row: RAGEvidence) -> dict[str, Any]:
    meta = row.doc_metadata if isinstance(row.doc_metadata, dict) else {}
    excerpt = " ".join(str(row.chunk_text or "").split())
    if len(excerpt) > 280:
        excerpt = excerpt[:277].rstrip() + "..."
    return {
        "id": row.id,
        "plan_id": row.plan_id,
        "run_id": row.run_id,
        "document": row.document_source,
        "source_type": row.source_type,
        "section": meta.get("section") or meta.get("heading") or meta.get("page"),
        "retrieval_timestamp": _json_safe(row.retrieval_timestamp),
        "query_used": row.query_used,
        "relevance_summary": excerpt,
        "consumer": meta.get("consumer") or "Safety / engineering evidence",
        "is_synthetic": bool(row.is_synthetic),
    }


def _lesson_eligibility_reason(item: LessonMemory, source_outcome: OperationalOutcome | None) -> str:
    if item.source_forecast_error_id:
        return "Created from evidence-backed forecast-error analysis."
    goal = None
    if source_outcome is not None:
        try:
            actual = json.loads(source_outcome.actual_outcome or "{}")
            if isinstance(actual, dict):
                goal = actual.get("goal_result")
        except (TypeError, ValueError):
            pass
    if str(goal or "").upper() == "ACHIEVED" and int(item.frequency_count or 0) >= 2:
        return f"Reinforced because {int(item.frequency_count)} comparable ACHIEVED outcomes were observed."
    if str(goal or "").upper() in {"PARTIAL", "NOT_ACHIEVED"}:
        return f"Created because an evidence-backed {str(goal).replace('_', ' ')} outcome qualified for reusable learning."
    return "Stored because the configured evidence and repeatability rules qualified this pattern for reuse."


def _run_with_trace(trigger_source: str, operation: Callable[[PlannerAgent], Any]):
    """Open trace -> execute through Planner -> close trace, consistently.

    API workflows use the same bounded deterministic planner budget configured for
    autonomous operations. The generic Planner default remains 16 for interactive
    LLM loops, while multi-stage production workflows such as execution +
    post-execution recovery use the existing automation budget.
    """
    memory = SystemMemoryManager()
    run = memory.create_agent_run_trace(trigger_source)
    context = ToolContext(db=db, session=db.session, run_id=run.id)

    automation_config = load_json("automation_v1.json")
    limit_cfg = automation_config.get("planner_limits") or {}
    default_limits = PlannerLimits()
    planner = PlannerAgent(
        context=context,
        limits=PlannerLimits(
            max_steps=max(1, int(limit_cfg.get("max_steps", default_limits.max_steps))),
            max_replans=max(0, int(limit_cfg.get("max_replans", default_limits.max_replans))),
            max_tool_calls=max(1, int(limit_cfg.get("max_tool_calls", default_limits.max_tool_calls))),
        ),
    )

    try:
        result = operation(planner)
        memory.close_agent_run_trace(run.id, "COMPLETED", "Request completed successfully")
        return jsonify({"ok": True, "message": "Request completed successfully", "run_id": run.id, "result": _json_safe(result), "errors": []})
    except Exception as exc:
        db.session.rollback()
        try:
            memory.close_agent_run_trace(run.id, "FAILED", str(exc))
        except Exception:
            db.session.rollback()
        raise


def api_errors(fn):
    """Return predictable JSON errors for frontend fetch() calls."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except LookupError as exc:
            return _error(str(exc), 404)
        except (TypeError, ValueError) as exc:
            db.session.rollback()
            return _error(str(exc), 400)
        except Exception as exc:
            db.session.rollback()
            # Keep internal ORM/SQL details out of the operator UI while preserving
            # the full traceback in the server log for engineering support.
            try:
                current_app.logger.exception("SolarGrid API operation failed: %s", exc)
            except Exception:
                pass
            return _error(
                "The operation could not be completed. The transaction was safely rolled back. "
                "Please retry once; if the problem continues, contact Technical Support.",
                500,
                code="OPERATION_FAILED",
            )

    return wrapper


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ValueError("JSON request body must be an object")
    return body


def _required_positive_int(body: dict[str, Any], key: str) -> int:
    if key not in body or body[key] is None:
        raise ValueError(f"'{key}' is required")
    return _positive_int(body[key], key)


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{name}' must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"'{name}' must be greater than 0")
    return parsed


def _get_plan_or_raise(plan_id: int) -> Plan:
    plan = db.session.get(Plan, plan_id)
    if plan is None:
        raise LookupError(f"Plan with id={plan_id} does not exist")
    return plan


def _operator_validation_message(plan: Plan, evaluation: PlanEvaluation | None) -> str:
    """Concise operator-facing explanation; raw engineering detail stays auditable."""
    if evaluation is None:
        return "Engineering validation has not completed for this candidate."
    if evaluation.is_feasible is True:
        return "Engineering checks passed for this planning snapshot."
    if evaluation.is_feasible is None:
        pf = evaluation.power_flow_result if isinstance(evaluation.power_flow_result, dict) else {}
        if pf.get("validation_error"):
            return "Engineering validation encountered a runtime issue. Retry validation; approval remains safely disabled until the checks complete."
        return "Some required engineering evidence is unavailable or stale; refresh validation before approval."

    messages: list[str] = []
    if evaluation.imbalance_mw is not None and abs(float(evaluation.imbalance_mw)) > 0.5:
        messages.append(
            f"Available resources cannot fully cover the forecast while preserving constraints; "
            f"worst remaining imbalance is {abs(float(evaluation.imbalance_mw)):.1f} MW."
        )
    if evaluation.reserve_margin_mw is not None and float(evaluation.reserve_margin_mw) < 0:
        messages.append(
            f"Operating reserve is short by {abs(float(evaluation.reserve_margin_mw)):.1f} MW."
        )
    violations = evaluation.constraint_violations if isinstance(evaluation.constraint_violations, list) else []
    if any(str(v.get("resource", "")).lower() == "network" for v in violations if isinstance(v, dict)):
        messages.append("Network limits also require attention.")

    # Surface one concise deterministic reason to the operator instead of
    # collapsing generator/battery failures into a generic message. Raw values
    # remain available in View Details / technical evidence.
    if not messages:
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            resource = str(violation.get("resource", "")).lower()
            constraint = str(violation.get("constraint", "")).lower()
            if resource == "generator" and constraint == "availability":
                messages.append("A candidate requests output from a generator that is unavailable.")
                break
            if resource == "generator" and constraint == "min_output_mw":
                messages.append("A generator dispatch falls below its permitted online minimum output.")
                break
            if resource == "generator" and constraint in {"ramp_rate_mw_per_min", "configured_max_change_mw"}:
                messages.append("A generator dispatch change exceeds its approved movement or ramp limit.")
                break
            if resource == "battery" and constraint in {"min_soc_pct", "max_soc_pct"}:
                messages.append("The battery trajectory would move outside its approved state-of-charge limits.")
                break
            if resource == "battery" and constraint in {"max_charge_mw", "max_discharge_mw", "availability"}:
                messages.append("The requested battery action exceeds an approved availability or power limit.")
                break

    if not messages and str(plan.optimization_status or "").upper() == "INFEASIBLE":
        messages.append("No dispatch solution satisfies all approved engineering constraints for this candidate.")
    return " ".join(messages) or "One or more deterministic engineering constraints failed."


def _repair_stale_standalone_runs() -> None:
    """Repair legacy TOOL_CALL rows left RUNNING by older runtime revisions.

    Only standalone runs that already have terminal tool-call logs are touched;
    caller-owned workflow runs are never modified here.
    """
    terminal = {"SUCCESS", "PARTIAL", "REJECTED", "FAILED", "TIMEOUT"}
    changed = False
    for run in AgentRunTrace.query.filter_by(status="RUNNING", trigger_source="TOOL_CALL").all():
        calls = list(run.tool_call_logs or [])
        if not calls or not all(str(call.status or "").upper() in terminal for call in calls):
            continue
        run.status = "FAILED" if any(str(call.status or "").upper() == "FAILED" for call in calls) else "COMPLETED"
        run.end_time = datetime.now(timezone.utc).replace(tzinfo=None)
        run.tools_called = [call.tool_name for call in calls]
        run.final_outcome_summary = "Reconciled legacy standalone tool trace."
        changed = True
    if changed:
        db.session.commit()


def _plan_dict(plan: Plan, detailed: bool = False) -> dict[str, Any]:
    data = _model_dict(plan)
    latest_evaluation = (
        PlanEvaluation.query.filter_by(plan_id=plan.id)
        .order_by(PlanEvaluation.id.desc())
        .first()
    )
    if latest_evaluation is None:
        validation_status = "NOT_VALIDATED"
        validation_reason = "No deterministic Tool 09 evaluation is stored."
        validation_snapshot_id = None
    else:
        if latest_evaluation.is_feasible is True:
            validation_status = "FEASIBLE"
        elif latest_evaluation.is_feasible is False:
            validation_status = "INFEASIBLE"
        else:
            validation_status = "UNKNOWN"
        validation_reason = latest_evaluation.rejection_reason
        validation_snapshot_id = plan.snapshot_id

    data["validation_status"] = validation_status
    data["validation_reason"] = validation_reason
    data["validation_operator_message"] = _operator_validation_message(plan, latest_evaluation)
    data["validation_snapshot_id"] = validation_snapshot_id
    data["latest_evaluation"] = _model_dict(latest_evaluation) if latest_evaluation else None
    data["dynamic_pricing"] = _dynamic_pricing_plan_summary(plan)
    data["can_approve"] = (
        plan.status == "PROPOSED"
        and latest_evaluation is not None
        and latest_evaluation.is_feasible is True
        and validation_snapshot_id is not None
        and int(validation_snapshot_id) == int(plan.snapshot_id)
    )
    source_run = db.session.get(AgentRunTrace, plan.run_id) if plan.run_id else None
    source_decisions = source_run.decisions if source_run is not None and isinstance(source_run.decisions, dict) else {}
    data["trigger_context"] = source_decisions.get("trigger_context") if isinstance(source_decisions.get("trigger_context"), dict) else None
    source_ids = source_decisions.get("external_data_provenance_ids") or []
    data["data_sources_used"] = source_ids
    details = []
    for source_id in source_ids:
        try:
            row = db.session.get(ExternalDataProvenance, int(source_id))
        except (TypeError, ValueError):
            row = None
        if row is not None:
            details.append({
                "id": row.id, "source": row.source, "status": row.status,
                "retrieved_at": _json_safe(row.retrieved_at), "location": row.location,
                "source_role": row.source_role,
            })
    data["data_source_details"] = details
    if detailed:
        data["evaluations"] = [_model_dict(item) for item in plan.evaluations]
        data["approval"] = _model_dict(plan.approval) if plan.approval else None
        data["execution"] = _model_dict(plan.execution) if plan.execution else None
    return data



def _dynamic_pricing_plan_summary(plan: Plan) -> dict[str, Any] | None:
    actions = plan.actions if isinstance(plan.actions, dict) else {}
    ref = actions.get("dynamic_pricing_context")
    if not isinstance(ref, dict) or ref.get("tool_call_id") is None:
        return None
    try:
        tool_call_id = int(ref["tool_call_id"])
    except (TypeError, ValueError):
        return {"tool_call_id": ref.get("tool_call_id"), "status": "INVALID_REFERENCE"}
    log = db.session.get(ToolCallLog, tool_call_id)
    if log is None or log.tool_name != "analyze_dynamic_pricing":
        return {"tool_call_id": tool_call_id, "status": "SOURCE_UNAVAILABLE"}
    output = log.output_json if isinstance(log.output_json, dict) else {}
    hourly = output.get("hourly_analysis") if isinstance(output.get("hourly_analysis"), list) else []
    priced = [row for row in hourly if isinstance(row, dict) and row.get("final_price_per_mwh") is not None]
    target_prices = [float(row["final_price_per_mwh"]) for row in priced]
    return {
        "tool_call_id": tool_call_id,
        "status": str(log.status or "UNKNOWN"),
        "energy_scope": output.get("energy_scope"),
        "action_required": output.get("action_required"),
        "total_shifted_mw": output.get("total_shifted_mw"),
        "target_interval_count": len(output.get("load_shift_entries") or []),
        "minimum_price_per_mwh": min(target_prices) if target_prices else None,
        "expected_impact": output.get("expected_impact") if isinstance(output.get("expected_impact"), dict) else {},
        "reasons": output.get("reasons") if isinstance(output.get("reasons"), list) else [],
        "warnings": output.get("warnings") if isinstance(output.get("warnings"), list) else [],
    }


def _model_dict(model: Any) -> dict[str, Any]:
    """Serialize SQLAlchemy columns only; relationships are added explicitly."""
    return {
        column.key: _json_safe(getattr(model, column.key))
        for column in sa_inspect(model).mapper.column_attrs
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    return str(value)


# Real-project identity metadata is centralized in solar_projects.py.
# Every shipped project is now a dataset-backed Generator row. The project
# identity/capacity metadata is reference information, while runtime output,
# availability and dispatch values remain synthetic demo data.

def _display_generation_plants() -> list[dict[str, Any]]:
    generators = Generator.query.order_by(Generator.id.asc()).all()
    rows: list[dict[str, Any]] = []
    for generator in generators:
        constraints = generator.constraints_json or {}
        project_key = constraints.get("project_key")
        meta = SOLAR_PROJECTS_BY_KEY.get(str(project_key)) if project_key else None
        if meta is None:
            meta = SOLAR_PROJECTS_BY_NAME.get(str(generator.name))
        if meta is None:
            # Unknown generator rows remain visible and operator-reviewable rather
            # than being silently dropped from the map.
            meta = {
                "project_key": str(project_key or f"GEN_{generator.id}"),
                "name": generator.name,
                "location": "Demo grid",
                "governorate": "Jordan",
                "lat": None,
                "lon": None,
                "capacity_mw": generator.capacity_mw,
                "source": "Dataset generator row; geographic metadata unavailable",
            }
        rows.append(_generation_plant_dict(generator, meta))
    return rows

def _plant_status(generator: Generator) -> tuple[str, list[str]]:
    return status_from_availability(generator.availability_status)

def _generation_plant_dict(generator: Generator, meta: dict[str, Any]) -> dict[str, Any]:
    status, reasons = _plant_status(generator)
    constraints = generator.constraints_json or {}
    return {
        "id": generator.id,
        "asset_code": f"G{generator.id}",
        "project_key": constraints.get("project_key") or meta.get("project_key"),
        "name": generator.name or meta["name"],
        "source_generator_name": generator.name,
        "technology": "SOLAR",
        "location": meta.get("location") or constraints.get("location"),
        "governorate": meta.get("governorate") or constraints.get("governorate"),
        "lat": meta.get("lat") if meta.get("lat") is not None else constraints.get("latitude"),
        "lon": meta.get("lon") if meta.get("lon") is not None else constraints.get("longitude"),
        "status": status,
        "status_reasons": reasons,
        "current_output_mw": generator.current_output_mw,
        "capacity_mw": generator.capacity_mw,
        "operating_limit_mw": generator.max_output_mw,
        "availability": generator.availability_status,
        "metadata_verified": False,
        "project_identity_verified": True,
        "coordinates_are_approximate": True,
        "metadata_source": meta.get("source") or constraints.get("reference_source"),
        "operational_values_synthetic": bool(generator.is_synthetic),
        "display_only": False,
        "dispatch_participant": True,
        "last_updated": _json_safe(generator.updated_at),
    }

def _validate_existing_plan(
    planner: PlannerAgent,
    *,
    plan_id: int,
    context_snapshot_id: int,
    decision_policy_version: str,
) -> dict[str, Any]:
    """Re-run Tools 06-09 for an existing plan using the current Planner boundary."""
    plan = db.session.get(Plan, plan_id)
    if plan is None:
        raise LookupError(f"Plan with id={plan_id} does not exist")
    if int(plan.snapshot_id) != int(context_snapshot_id):
        raise ValueError(
            "Existing plans can only be validated against their own planning snapshot. "
            "Use Recheck Plan for a newer snapshot."
        )
    dispatch = plan.actions if isinstance(plan.actions, dict) else {}
    interval_minutes = int(dispatch.get("interval_minutes") or 15)
    intervals = dispatch.get("intervals") or []
    horizon_minutes = interval_minutes * len(intervals)
    if horizon_minutes <= 0:
        horizon_minutes = max(1, int(plan.horizon_hours or 2) * 60)

    context = planner.resolve_validation_context(
        snapshot_id=context_snapshot_id,
        horizon_minutes=horizon_minutes,
        decision_policy_version=decision_policy_version,
        ensure_reserve=True,
    )

    gen = planner._call("check_generator_constraints", {
        "plan_id": plan_id, "context_snapshot_id": context_snapshot_id
    })
    bat = planner._call("check_battery_constraints", {
        "plan_id": plan_id, "context_snapshot_id": context_snapshot_id
    })
    network = planner._call("run_power_flow", {
        "plan_id": plan_id,
        "network_id": context["network_id"],
        "context_snapshot_id": context_snapshot_id,
    })
    evaluated = planner._call("evaluate_plans", {
        "plan_ids": [plan_id],
        "decision_policy_version": decision_policy_version,
        "reserve_assessment_id": context["reserve_assessment_id"],
        "context_snapshot_id": context_snapshot_id,
        "network_id": context["network_id"],
        "horizon_minutes": horizon_minutes,
    })
    return {
        "generator_check": gen,
        "battery_check": bat,
        "network_check": network,
        "evaluation": evaluated,
    }

def _error(message: str, status_code: int, *, code: str | None = None):
    # Keep the legacy ``error`` field for current UI compatibility while also
    # returning the normalized API envelope used by newer callers.
    payload = {
        "ok": False,
        "message": message,
        "error": message,
        "result": None,
        "errors": [message],
    }
    if code:
        payload["code"] = code
    return jsonify(payload), status_code


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=_env_bool("FLASK_DEBUG", False),
        threaded=True,
        use_reloader=False,
    )
