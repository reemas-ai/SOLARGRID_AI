"""Single execution boundary for SOLARGRID AI tools 01-16.

The planner must never call tool implementations directly.  This module owns
input validation, dependency injection and the small signature differences
between the three existing tool groups.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from database import (
    AgentRunTrace, ToolCallLog, SystemSnapshot, Forecast, ActualMeasurement,
    ForecastErrorLog, GridBus, GridLine, Generator, Battery, Load,
    ReserveAssessment, ImbalanceEvent, Plan, PlanEvaluation, Scenario,
    EvaluationRun, MonitoringLog, HumanApproval, PlanExecution,
    OperationalOutcome, RAGEvidence, LessonMemory, ChangeImpactAssessment,
)
from schemas import (
    GetSystemStateRequest, ReserveRequest, ImbalanceRequest, FutureRiskRequest,
    GeneratePlansRequest, PlanValidationRequest, PowerFlowRequest,
    PlanEvaluationRequest, ScenarioRequest, MonitorRequest, ChangeImpactRequest,
    ExecutePlanRequest, OutcomeDiagnosisRequest, ForecastErrorRequest,
    EvidenceRequest, DynamicPricingRequest,
)
from tool_registry import get_tool
from tools.operational_tools import bind_run_id


@dataclass(frozen=True)
class ToolContext:
    """Dependencies supplied by the application, not invented by the LLM."""
    db: Any
    session: Any
    run_id: int | None = None
    policy_loader: Callable | None = None


MODELS = {
    name: value for name, value in {
        "AgentRunTrace": AgentRunTrace,
        "ToolCallLog": ToolCallLog,
        "SystemSnapshot": SystemSnapshot,
        "Forecast": Forecast,
        "ActualMeasurement": ActualMeasurement,
        "ForecastErrorLog": ForecastErrorLog,
        "GridBus": GridBus,
        "GridLine": GridLine,
        "Generator": Generator,
        "Battery": Battery,
        "Load": Load,
        "ReserveAssessment": ReserveAssessment,
        "ImbalanceEvent": ImbalanceEvent,
        "Plan": Plan,
        "PlanEvaluation": PlanEvaluation,
        "Scenario": Scenario,
        "EvaluationRun": EvaluationRun,
        "MonitoringLog": MonitoringLog,
        "HumanApproval": HumanApproval,
        "PlanExecution": PlanExecution,
        "OperationalOutcome": OperationalOutcome,
        "RAGEvidence": RAGEvidence,
        "LessonMemory": LessonMemory,
        "ChangeImpactAssessment": ChangeImpactAssessment,
    }.items()
}

REQUEST_SCHEMAS = {
    "get_system_state": GetSystemStateRequest,
    "calculate_reserve": ReserveRequest,
    "detect_imbalance": ImbalanceRequest,
    "assess_future_risk": FutureRiskRequest,
    "generate_and_optimize_plans": GeneratePlansRequest,
    "check_generator_constraints": PlanValidationRequest,
    "check_battery_constraints": PlanValidationRequest,
    "run_power_flow": PowerFlowRequest,
    "evaluate_plans": PlanEvaluationRequest,
    "run_scenario_analysis": ScenarioRequest,
    "monitor_system_conditions": MonitorRequest,
    "assess_change_impact": ChangeImpactRequest,
    "execute_and_verify_plan": ExecutePlanRequest,
    "assess_and_diagnose_outcome": OutcomeDiagnosisRequest,
    "forecast_error_analysis": ForecastErrorRequest,
    "retrieve_engineering_evidence": EvidenceRequest,
    "analyze_dynamic_pricing": DynamicPricingRequest,
}

# Actual implementation signatures, deliberately kept explicit so that a
# future refactor cannot silently turn the LLM into an arbitrary Python caller.
TOOL_CATEGORIES = {
    **{name: "STATE_ANALYSIS" for name in list(REQUEST_SCHEMAS)[:4]},
    **{name: "PLANNING_VALIDATION" for name in list(REQUEST_SCHEMAS)[4:9]},
    "run_scenario_analysis": "SCENARIO",
    "monitor_system_conditions": "MONITORING",
    "assess_change_impact": "REVALIDATION",
    "execute_and_verify_plan": "EXECUTION",
    "assess_and_diagnose_outcome": "OUTCOME",
    "forecast_error_analysis": "LEARNING",
    "retrieve_engineering_evidence": "EVIDENCE",
    "analyze_dynamic_pricing": "DYNAMIC_PRICING",
}


def validate_tool_input(name: str, payload: dict[str, Any]):
    schema = REQUEST_SCHEMAS.get(name)
    if schema is None:
        raise KeyError(f"No request schema registered for tool: {name}")
    return schema.model_validate(payload)


def _planning_payload(request) -> dict[str, Any]:
    return request.model_dump(mode="json")


def execute_tool(name: str, payload: dict[str, Any], *, context: ToolContext) -> Any:
    """Validate and execute exactly one registered tool.

    No planning decision is made here.  This function is a dispatcher only.
    """
    request = validate_tool_input(name, payload)
    tool = get_tool(name)

    if name == "get_system_state":
        return tool(
            context.session,
            run_id=context.run_id,
            snapshot_time=request.timestamp,
            forecast_horizon_minutes=request.horizon_minutes or 120,
            external_weather_record_id=request.external_weather_record_id,
        )

    if name == "calculate_reserve":
        return tool(
            context.session,
            request.snapshot_id,
            request.horizon_minutes,
            policy_loader=context.policy_loader,
            run_id=context.run_id,
        )

    if name == "detect_imbalance":
        return tool(
            context.session,
            request.snapshot_id,
            policy_loader=context.policy_loader,
            run_id=context.run_id,
        )

    if name == "assess_future_risk":
        return tool(
            context.session,
            request.snapshot_id,
            request.horizon_minutes,
            policy_loader=context.policy_loader,
            run_id=context.run_id,
        )

    if name in {"generate_and_optimize_plans", "check_generator_constraints",
                "check_battery_constraints", "run_power_flow", "evaluate_plans"}:
        return tool(
            _planning_payload(request),
            db=context.db,
            models=MODELS,
            run_id=context.run_id,
        )

    if name == "analyze_dynamic_pricing":
        return tool(
            request,
            db=context.db,
            models=MODELS,
            run_id=context.run_id,
        )

    if name in {
        "run_scenario_analysis", "monitor_system_conditions",
        "assess_change_impact", "execute_and_verify_plan",
        "assess_and_diagnose_outcome", "forecast_error_analysis",
        "retrieve_engineering_evidence",
    }:
        # The application/Planner context is the canonical trace when present.
        # A request-level run_id is accepted only as a fallback for direct callers
        # that do not provide a ToolContext run_id.
        request_run_id = getattr(request, "run_id", None)
        effective_run_id = context.run_id if context.run_id is not None else request_run_id

        with bind_run_id(effective_run_id):
            if name == "run_scenario_analysis":
                return tool(
                    request.base_snapshot_id,
                    request.horizon_minutes,
                    solar_delta_pct=request.solar_delta_pct,
                    demand_delta_pct=request.demand_delta_pct,
                    generator_outages=request.generator_outages,
                    other_changes=request.other_changes,
                )

            if name == "monitor_system_conditions":
                return tool(
                    request.active_plan_id,
                    request.current_snapshot_id,
                    threshold_profile=request.threshold_profile,
                )

            if name == "assess_change_impact":
                return tool(request.plan_id, request.current_snapshot_id, request.monitoring_log_id)

            if name == "execute_and_verify_plan":
                return tool(request.plan_id, request.approval_id, request.execution_mode)

            if name == "assess_and_diagnose_outcome":
                return tool(
                    request.execution_id,
                    pre_execution_snapshot_id=request.pre_execution_snapshot_id,
                    post_execution_snapshot_id=request.post_execution_snapshot_id,
                    balance_tolerance_mw=request.balance_tolerance_mw,
                )

            if name == "forecast_error_analysis":
                return tool(
                    forecast_ids=request.forecast_ids,
                    actual_measurement_ids=request.actual_measurement_ids,
                    lookback_days=request.lookback_days,
                    time_window_start=request.time_window_start,
                    time_window_end=request.time_window_end,
                    variable_name=request.variable_name,
                    matching_profile=request.matching_profile,
                )

            if name == "retrieve_engineering_evidence":
                return tool(
                    request.query,
                    request.top_k,
                    request.plan_id,
                    effective_run_id,
                    request.topic,
                )

    raise KeyError(f"Dispatcher has no implementation adapter for: {name}")
