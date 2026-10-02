"""
SOLARGRID AI — Shared Pydantic Schemas
Version: 1.0 / implementation handoff

This file is the single shared programmatic contract for public tool boundaries.
Individual tools MUST import models from this module and MUST NOT create duplicate
public request/response schemas.

Important:
- UNKNOWN/NULL is preserved; do not coerce unknown values to zero/False/GOOD.
- Tool status and domain status are separate concepts.
- Physical calculations remain deterministic and live in tools/simulation/solvers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------

class ToolStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class DomainStatus(str, Enum):
    OK = "OK"
    UNKNOWN = "UNKNOWN"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_EVALUATED = "NOT_EVALUATED"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"


class PlanStatus(str, Enum):
    """Authoritative Plan.status values from the v1.1 contract."""
    PROPOSED = "PROPOSED"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    STALE = "STALE"
    SUPERSEDED = "SUPERSEDED"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class NetworkValidationStatus(str, Enum):
    NOT_EVALUATED = "NOT_EVALUATED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class ExecutionStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_FAIL = "PARTIAL_FAIL"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class ImbalanceDirection(str, Enum):
    DEFICIT = "DEFICIT"
    SURPLUS = "SURPLUS"
    BALANCED = "BALANCED"
    UNKNOWN = "UNKNOWN"


class ImpactLevel(str, Enum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class Strategy(str, Enum):
    SOLAR_ONLY = "SOLAR_ONLY"
    BATTERY_ONLY = "BATTERY_ONLY"
    MIXED = "MIXED"
    COST_MIN = "COST_MIN"
    RESERVE_PRESERVING = "RESERVE_PRESERVING"


class PowerFlowStatus(str, Enum):
    NOT_EVALUATED = "NOT_EVALUATED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONVERGED = "CONVERGED"
    NON_CONVERGED = "NON_CONVERGED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Common envelope
# ---------------------------------------------------------------------------

class ToolEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_status: ToolStatus
    tool_name: str
    tool_version: str = "1.0"
    run_id: Optional[str] = None
    timestamp: datetime
    result: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared physical/state models
# ---------------------------------------------------------------------------

class ForecastPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variable_name: str
    forecast_value: Optional[float] = None
    unit: str
    source: str
    issued_at: datetime
    target_time: datetime
    confidence_lower: Optional[float] = None
    confidence_upper: Optional[float] = None


class GeneratorState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generator_id: int | str
    name: str
    capacity_mw: float
    current_output_mw: Optional[float] = None
    min_output_mw: Optional[float] = None
    max_output_mw: Optional[float] = None
    ramp_rate_mw_per_min: Optional[float] = None
    availability_status: Optional[str] = None
    constraints: Dict[str, Any] = Field(default_factory=dict)


class BatteryState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    battery_id: int | str
    name: str
    capacity_mwh: float
    soc_pct: Optional[float] = None
    min_soc_pct: Optional[float] = None
    max_soc_pct: Optional[float] = None
    max_charge_mw: Optional[float] = None
    max_discharge_mw: Optional[float] = None
    efficiency: Optional[float] = None
    current_power_mw: Optional[float] = None
    availability_status: Optional[str] = None


class LoadState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    load_id: int | str
    name: str
    p_mw: Optional[float] = None
    q_mvar: Optional[float] = None
    load_type: Optional[str] = None
    is_flexible: Optional[bool] = None


class GridState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    network_id: Optional[str] = None
    transfer_limit_mw: Optional[float] = None


class WeatherState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Optional[str] = None
    observed_at: Optional[datetime] = None
    values: Dict[str, Any] = Field(default_factory=dict)


class SystemState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: Optional[int] = None
    timestamp: datetime
    current_time: datetime
    solar_generation_mw: Optional[float] = None
    demand_mw: Optional[float] = None
    other_generation_mw: Optional[float] = None
    battery_soc_pct: Optional[float] = None
    reserve_margin_mw: Optional[float] = None
    grid_status: Optional[str] = None
    data_quality: Optional[str] = None
    data_source: Optional[str] = None
    is_synthetic: Optional[bool] = None
    weather: Optional[WeatherState] = None
    generators: List[GeneratorState] = Field(default_factory=list)
    batteries: List[BatteryState] = Field(default_factory=list)
    loads: List[LoadState] = Field(default_factory=list)
    forecasts: List[ForecastPoint] = Field(default_factory=list)
    state_json: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool 01 — state
# ---------------------------------------------------------------------------

class GetSystemStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: Optional[datetime] = None
    horizon_minutes: Optional[int] = Field(default=None, ge=0)
    external_weather_record_id: Optional[int] = Field(default=None, gt=0)


class GetSystemStateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: Optional[int] = None
    state: SystemState
    domain_status: DomainStatus = DomainStatus.OK


# ---------------------------------------------------------------------------
# Tool 02 — reserve
# ---------------------------------------------------------------------------

class ReserveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int
    horizon_minutes: int = Field(ge=0)


class ReserveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int
    required_reserve_mw: Optional[float] = None
    actual_reserve_mw: Optional[float] = None
    generator_up_reserve_mw: Optional[float] = None
    battery_up_reserve_mw: Optional[float] = None
    generator_down_reserve_mw: Optional[float] = None
    battery_down_reserve_mw: Optional[float] = None
    limiting_constraints: Dict[str, Any] = Field(default_factory=dict)
    horizon_minutes: int
    domain_status: DomainStatus


# ---------------------------------------------------------------------------
# Tool 03 — imbalance
# ---------------------------------------------------------------------------

class ImbalanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int


class ImbalanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int
    direction: ImbalanceDirection
    imbalance_mw: Optional[float] = None
    severity: Optional[str] = None
    drivers: List[str] = Field(default_factory=list)
    reserve_adequate: Optional[bool] = None
    event_required: Optional[bool] = None
    domain_status: DomainStatus


# ---------------------------------------------------------------------------
# Tool 04 — future risk
# ---------------------------------------------------------------------------

class FutureRiskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int
    horizon_minutes: int = Field(ge=0)


class RiskPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_time: datetime
    risk_level: str
    expected_imbalance_mw: Optional[float] = None
    drivers: List[str] = Field(default_factory=list)


class FutureRiskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int
    horizon_minutes: int
    risk_points: List[RiskPoint] = Field(default_factory=list)
    domain_status: DomainStatus


# ---------------------------------------------------------------------------
# Canonical dispatch used by Tools 05–10 and lifecycle
# ---------------------------------------------------------------------------

class DynamicPricingPlanningContextRef(BaseModel):
    """Trusted reference to a completed Tool 17 analysis.

    The canonical plan persists only the ToolCallLog provenance reference, not
    caller-supplied engineering values. Validation/execution layers must reload
    and verify the referenced Tool 17 result before using its demand adjustments.
    """
    model_config = ConfigDict(extra="forbid")

    tool_call_id: int = Field(gt=0)


class GeneratorDispatchPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output_mw: Optional[float] = None


class BatteryDispatchPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    power_mw: Optional[float] = None


class DispatchInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    start: datetime
    generators: Dict[str, GeneratorDispatchPoint] = Field(default_factory=dict)
    batteries: Dict[str, BatteryDispatchPoint] = Field(default_factory=dict)
    solar_curtailment_mw: Optional[float] = None
    residual_imbalance_mw: Optional[float] = None


class PlanDispatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    interval_minutes: int = Field(gt=0)
    start_time: datetime
    intervals: List[DispatchInterval] = Field(min_length=1)
    dynamic_pricing_context: Optional[DynamicPricingPlanningContextRef] = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def interval_consistency(self):
        expected = self.start_time
        for expected_index, item in enumerate(self.intervals):
            if item.index != expected_index:
                raise ValueError("Dispatch interval indexes must be contiguous from zero")
            if item.start != expected:
                raise ValueError("Dispatch interval start times must be contiguous")
            expected = expected + timedelta(minutes=self.interval_minutes)
        return self


# ---------------------------------------------------------------------------
# Tool 05 — plan generation/optimization
# ---------------------------------------------------------------------------

class GeneratePlansRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int
    risk_event_id: Optional[int] = None
    reserve_assessment_id: Optional[int] = None
    horizon_minutes: int = Field(gt=0)
    objectives: List[str] = Field(default_factory=list)
    decision_policy_version: str = "v1.0-demo"
    required_candidate_count: int = Field(gt=0)
    strategies: Optional[List[Strategy]] = None
    dynamic_pricing_context: Optional[DynamicPricingPlanningContextRef] = None


class CandidatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: Optional[int] = None
    candidate_id: str
    plan_name: str
    strategy: Strategy
    dispatch: PlanDispatch
    generator_dispatch: Dict[str, Any] = Field(default_factory=dict)
    battery_dispatch: Dict[str, Any] = Field(default_factory=dict)
    expected_balance_mw: Optional[float] = None
    expected_reserve_mw: Optional[float] = None
    objective_metrics: Dict[str, Any] = Field(default_factory=dict)
    assumptions: List[str] = Field(default_factory=list)
    optimization_status: str
    solver_status: Optional[str] = None
    hard_constraint_slack_mw: Optional[float] = None
    domain_status: DomainStatus = DomainStatus.NOT_EVALUATED


class GeneratePlansResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int
    plans: List[CandidatePlan]
    domain_status: DomainStatus


# ---------------------------------------------------------------------------
# Tools 06/07 — independent resource validators
# ---------------------------------------------------------------------------

class PlanValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    # Optional validation context. When omitted, validators preserve the original
    # plan snapshot behavior. Tool 12 supplies the current snapshot explicitly
    # so the same plan can be revalidated without mutating the Plan row.
    context_snapshot_id: Optional[int] = None


class ConstraintViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str
    resource_id: Optional[str] = None
    interval_index: Optional[int] = None
    constraint: str
    observed: Optional[float | str] = None
    requested: Optional[float | str] = None
    limit: Optional[float | str] = None
    excess: Optional[float] = None
    message: str


class GeneratorValidationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generator_id: str
    interval_results: List[Dict[str, Any]] = Field(default_factory=list)
    requested_output_mw: Optional[float] = None
    feasible: Optional[bool] = None
    violations: List[ConstraintViolation] = Field(default_factory=list)
    maximum_feasible_change_mw: Optional[float] = None


class GeneratorValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    aggregate_feasible: Optional[bool] = None
    generators: List[GeneratorValidationItem] = Field(default_factory=list)
    domain_status: DomainStatus
    checked_at: datetime


class BatteryValidationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    battery_id: str
    soc_path_pct: List[Optional[float]] = Field(default_factory=list)
    requested_power_mw: Optional[float] = None
    soc_before_pct: Optional[float] = None
    soc_after_pct: Optional[float] = None
    feasible: Optional[bool] = None
    violations: List[ConstraintViolation] = Field(default_factory=list)


class BatteryValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    aggregate_feasible: Optional[bool] = None
    batteries: List[BatteryValidationItem] = Field(default_factory=list)
    domain_status: DomainStatus
    checked_at: datetime


class ResourceValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    is_feasible: Optional[bool] = None
    violations: List[ConstraintViolation] = Field(default_factory=list)
    domain_status: DomainStatus
    checked_at: datetime


# ---------------------------------------------------------------------------
# Tool 08 — network validation
# ---------------------------------------------------------------------------

class PowerFlowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    network_id: Optional[str] = None
    context_snapshot_id: Optional[int] = None
    simulation_options: Dict[str, Any] = Field(default_factory=dict)


class PowerFlowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    network_id: Optional[str] = None
    status: PowerFlowStatus
    converged: Optional[bool] = None
    is_network_feasible: Optional[bool] = None
    bus_voltage_results: List[Dict[str, Any]] = Field(default_factory=list)
    line_loading_results: List[Dict[str, Any]] = Field(default_factory=list)
    transformer_loading_results: List[Dict[str, Any]] = Field(default_factory=list)
    thermal_violations: List[Dict[str, Any]] = Field(default_factory=list)
    voltage_violations: List[Dict[str, Any]] = Field(default_factory=list)
    congestion: List[Dict[str, Any]] = Field(default_factory=list)
    validated_demand_mw: List[float] = Field(default_factory=list)
    dynamic_pricing_tool_call_id: Optional[int] = None
    solver_warnings: List[str] = Field(default_factory=list)
    solver_errors: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool 09 — evaluation
# ---------------------------------------------------------------------------

class PlanEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_ids: List[int] = Field(min_length=1)
    decision_policy_version: str
    reserve_assessment_id: int | None = None
    # Authoritative validation context. These optional fields preserve backward
    # compatibility for direct Tool 09 callers while allowing the Planner to
    # keep Tools 06-09 on the exact same snapshot/network/horizon.
    context_snapshot_id: Optional[int] = None
    network_id: Optional[str] = None
    horizon_minutes: Optional[int] = Field(default=None, gt=0)


class PlanEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    hard_feasible: Optional[bool] = None
    is_feasible: Optional[bool] = None
    violations: List[ConstraintViolation] = Field(default_factory=list)
    power_flow_feasible: Optional[bool] = None
    network_validation_status: NetworkValidationStatus = NetworkValidationStatus.NOT_EVALUATED
    reserve_margin_mw: Optional[float] = None
    residual_imbalance_mw: Optional[float] = None
    imbalance_mw: Optional[float] = None
    estimated_cost: Optional[float] = None
    validated_demand_mw: List[float] = Field(default_factory=list)
    dynamic_pricing_tool_call_id: Optional[int] = None
    tradeoffs: List[str] = Field(default_factory=list)
    rejection_reason: Optional[str] = None
    decision_policy_version: str


class EvaluatePlansResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluations: List[PlanEvaluationResult]
    decision_summary: Dict[str, Any] = Field(default_factory=dict)
    selected_plan_id: Optional[int] = None
    selection_reason: Optional[str] = None
    domain_status: DomainStatus


# ---------------------------------------------------------------------------
# Tool 10 — scenario analysis
# ---------------------------------------------------------------------------

class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Public Tool 10 contract: only fields consumed by run_scenario_analysis.
    base_snapshot_id: int
    horizon_minutes: int = Field(gt=0)
    solar_delta_pct: Optional[float] = None
    demand_delta_pct: Optional[float] = None
    generator_outages: List[str] = Field(default_factory=list)
    other_changes: Dict[str, Any] = Field(default_factory=dict)


class ScenarioResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: Optional[int] = None
    base_snapshot_id: int
    changed_assumptions: Dict[str, Any] = Field(default_factory=dict)
    resulting_state: Dict[str, Any] = Field(default_factory=dict)
    risk: Dict[str, Any] = Field(default_factory=dict)
    reserve: Dict[str, Any] = Field(default_factory=dict)
    plan_results: List[Dict[str, Any]] = Field(default_factory=list)
    constraint_results: Dict[str, Any] = Field(default_factory=dict)
    power_flow: Dict[str, Any] = Field(default_factory=dict)
    comparison_to_base: Dict[str, Any] = Field(default_factory=dict)
    domain_status: DomainStatus


# ---------------------------------------------------------------------------
# Tools 11/12 — monitoring and impact
# ---------------------------------------------------------------------------

class MonitorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_plan_id: int
    current_snapshot_id: int
    threshold_profile: Optional[Any] = None


class MonitoringResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_plan_id: int
    change_detected: bool
    threshold_crossed: bool
    trigger_type: Optional[str] = None
    magnitude_of_change: Optional[float] = None
    old_state: Dict[str, Any] = Field(default_factory=dict)
    new_state: Dict[str, Any] = Field(default_factory=dict)
    domain_status: DomainStatus


class ChangeImpactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    current_snapshot_id: int
    monitoring_log_id: Optional[int] = None


class ChangeImpactResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_plan_id: int
    current_snapshot_id: int
    impact_level: ImpactLevel
    affected_assumptions: List[str] = Field(default_factory=list)
    affected_actions: List[str] = Field(default_factory=list)
    operational_effect: Optional[str] = None
    still_valid: Optional[bool] = None
    replan_required: bool
    reason: str
    metrics: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool 13 — execution
# ---------------------------------------------------------------------------

class ExecutePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    approval_id: int
    execution_mode: str = "SIMULATION"


class PlanExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: Optional[int] = None
    plan_id: int
    status: ExecutionStatus
    requested_actions: Dict[str, Any] = Field(default_factory=dict)
    actual_actions: Dict[str, Any] = Field(default_factory=dict)
    deviations: Dict[str, Any] = Field(default_factory=dict)
    failure_info: Optional[Dict[str, Any]] = None
    execution_succeeded: Optional[bool] = None
    verification_delay_minutes: Optional[int] = None
    verification_due_at: Optional[datetime] = None
    dynamic_pricing_execution: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Tool 14 — outcome diagnosis
# ---------------------------------------------------------------------------

class OutcomeDiagnosisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: int
    pre_execution_snapshot_id: Optional[int] = None
    post_execution_snapshot_id: Optional[int] = None
    balance_tolerance_mw: Optional[float] = None


class OutcomeDiagnosisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: int
    execution_succeeded: Optional[bool] = None
    actions_matched_request: Optional[bool] = None
    operational_success: Optional[bool] = None
    goal_result: Optional[str] = None
    goal_criteria: Dict[str, Any] = Field(default_factory=dict)
    expected_outcome: Optional[str] = None
    actual_outcome: Optional[str] = None
    causes: List[str] = Field(default_factory=list)
    operational_impact: Optional[str] = None
    planning_implications: List[str] = Field(default_factory=list)
    verification_delay_minutes: Optional[int] = None
    verification_due_at: Optional[datetime] = None
    verification_status: Optional[str] = None
    dynamic_pricing_outcome: Optional[DynamicPricingOutcomeMetrics] = None


# ---------------------------------------------------------------------------
# Tool 15 — forecast error
# ---------------------------------------------------------------------------

class ForecastErrorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forecast_ids: Optional[List[int]] = None
    actual_measurement_ids: Optional[List[int]] = None
    lookback_days: Optional[float] = None
    time_window_start: Optional[datetime] = None
    time_window_end: Optional[datetime] = None
    variable_name: Optional[str] = None
    matching_profile: Optional[Dict[str, Any]] = None


class ForecastErrorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matched_pairs: int
    signed_errors: List[float] = Field(default_factory=list)
    absolute_errors: List[float] = Field(default_factory=list)
    error_percentages: List[Optional[float]] = Field(default_factory=list)
    pattern_label: Optional[str] = None
    known_cause: Optional[str] = None
    operational_impact: Optional[str] = None
    lesson_created: bool = False
    lesson_memory_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Tool 16 — RAG
# ---------------------------------------------------------------------------

class EvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    top_k: int = Field(default=3, gt=0)
    plan_id: Optional[int] = None
    run_id: Optional[int] = None
    topic: Optional[str] = None


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_source: str
    source_type: Optional[str] = None
    doc_metadata: Dict[str, Any] = Field(default_factory=dict)
    chunk_text: str
    query_used: str
    retrieval_timestamp: datetime
    citation: Optional[str] = None


class EvidenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[EvidenceItem] = Field(default_factory=list)
    domain_status: DomainStatus

# ---------------------------------------------------------------------------
# Tool 17 — Dynamic Pricing analysis (Solar-only)
# ---------------------------------------------------------------------------

class DynamicPricingSeverity(str, Enum):
    """Feature-local solar-surplus severity; not a Plan lifecycle status."""
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class DynamicPricingHourAnalysis(BaseModel):
    """Deterministic per-interval analysis returned by Tool 17.

    SolarGrid is intentionally solar-only. No wind field is part of this
    public contract. Unknown source data remains None rather than being
    coerced to zero.
    """
    model_config = ConfigDict(extra="forbid")

    interval_index: int = Field(ge=0)
    target_time: datetime
    solar_generation_mw: Optional[float] = Field(default=None, ge=0)
    demand_mw: Optional[float] = Field(default=None, ge=0)
    other_generation_mw: Optional[float] = Field(default=None, ge=0)
    battery_charging_headroom_mw: Optional[float] = Field(default=None, ge=0)
    export_capacity_mw: Optional[float] = Field(default=None, ge=0)
    flexible_load_mw: Optional[float] = Field(default=None, ge=0)
    max_shiftable_load_mw: Optional[float] = Field(default=None, ge=0)
    baseline_price_per_mwh: Optional[float] = Field(default=None, ge=0)
    solar_surplus_mw: Optional[float] = Field(default=None, ge=0)
    surplus_ratio: Optional[float] = Field(default=None, ge=0)
    curtailment_risk: Optional[bool] = None
    severity: DynamicPricingSeverity = DynamicPricingSeverity.UNKNOWN
    discount_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    final_price_per_mwh: Optional[float] = Field(default=None, ge=0)
    expected_flexible_response_mw: Optional[float] = Field(default=None, ge=0)


class DynamicPricingLoadShiftEntry(BaseModel):
    """Conserved source-to-target flexible-load movement proposed by Tool 17."""
    model_config = ConfigDict(extra="forbid")

    source_interval_index: int = Field(ge=0)
    source_time: datetime
    target_interval_index: int = Field(ge=0)
    target_time: datetime
    shifted_mw: float = Field(gt=0)

    @model_validator(mode="after")
    def source_and_target_must_differ(self):
        if self.source_interval_index == self.target_interval_index:
            raise ValueError("Dynamic Pricing source and target intervals must differ")
        if self.source_time == self.target_time:
            raise ValueError("Dynamic Pricing source and target times must differ")
        return self


class DynamicPricingDemandAdjustment(BaseModel):
    """Net demand delta consumed later by the existing planning pipeline.

    Negative means load leaves a source interval; positive means load is added
    to a target interval. Tool 17 must return an energy-conserving set.
    """
    model_config = ConfigDict(extra="forbid")

    interval_index: int = Field(ge=0)
    target_time: datetime
    demand_delta_mw: float


class DynamicPricingExpectedImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_load_shift_mw: Optional[float] = Field(default=None, ge=0)
    expected_surplus_reduction_mw: Optional[float] = Field(default=None, ge=0)
    expected_curtailment_reduction_mw: Optional[float] = Field(default=None, ge=0)
    expected_utilization_improvement_ratio: Optional[float] = Field(default=None, ge=0)


class DynamicPricingOutcomeMetrics(BaseModel):
    """Tool 14 expected-vs-actual Dynamic Pricing impact evidence.

    Dynamic Pricing outcome is deliberately separate from the existing grid
    balance/reserve goal_result.  Missing evidence remains None and cannot be
    silently converted into a successful impact claim.
    """
    model_config = ConfigDict(extra="forbid")

    tool_call_id: Optional[int] = Field(default=None, gt=0)
    verification_status: str
    goal_result: str
    expected_load_shift_mw: Optional[float] = Field(default=None, ge=0)
    actual_load_shift_mw: Optional[float] = Field(default=None, ge=0)
    expected_surplus_reduction_mw: Optional[float] = Field(default=None, ge=0)
    actual_surplus_reduction_mw: Optional[float] = Field(default=None, ge=0)
    expected_curtailment_reduction_mw: Optional[float] = Field(default=None, ge=0)
    actual_curtailment_reduction_mw: Optional[float] = Field(default=None, ge=0)
    expected_utilization_improvement_ratio: Optional[float] = Field(default=None, ge=0)
    actual_utilization_improvement_ratio: Optional[float] = Field(default=None, ge=0)
    deviations: Dict[str, Optional[float]] = Field(default_factory=dict)
    deviation_tolerance_ratio: Optional[float] = Field(default=None, ge=0)
    significant_deviation: Optional[bool] = None
    lesson_code: Optional[str] = None
    lesson_signal: Optional[str] = None
    evidence_basis: List[str] = Field(default_factory=list)


class DynamicPricingRequest(BaseModel):
    """Tool 17 request.

    The request deliberately accepts only authoritative context identifiers and
    does not accept raw solar/demand/price engineering values from the LLM.
    Tool 17 will resolve those values from the snapshot/config in Phase 2.
    """
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int = Field(gt=0)
    horizon_minutes: int = Field(gt=0)
    config_version: str = "v1.0-demo"


class DynamicPricingResult(BaseModel):
    """Public Tool 17 result contract; analysis only, never execution/approval."""
    model_config = ConfigDict(extra="forbid")

    # Populated by the public Tool 17 adapter after its ToolCallLog row exists.
    # The deterministic core leaves this None. Tool 05 uses the persisted ID as
    # the only accepted provenance reference for Dynamic Pricing planning input.
    tool_call_id: Optional[int] = Field(default=None, gt=0)
    snapshot_id: int = Field(gt=0)
    horizon_minutes: int = Field(gt=0)
    config_version: str
    energy_scope: Literal["SOLAR_ONLY"] = "SOLAR_ONLY"
    hourly_analysis: List[DynamicPricingHourAnalysis] = Field(default_factory=list)
    load_shift_entries: List[DynamicPricingLoadShiftEntry] = Field(default_factory=list)
    demand_adjustments: List[DynamicPricingDemandAdjustment] = Field(default_factory=list)
    total_shifted_mw: Optional[float] = Field(default=None, ge=0)
    expected_impact: DynamicPricingExpectedImpact = Field(default_factory=DynamicPricingExpectedImpact)
    action_required: Optional[bool] = None
    reasons: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    domain_status: DomainStatus

    @model_validator(mode="after")
    def validate_dynamic_pricing_invariants(self):
        tolerance = 1e-6

        hour_indexes = [item.interval_index for item in self.hourly_analysis]
        if len(hour_indexes) != len(set(hour_indexes)):
            raise ValueError("Dynamic Pricing hourly analysis interval indexes must be unique")

        adjustment_indexes = [item.interval_index for item in self.demand_adjustments]
        if len(adjustment_indexes) != len(set(adjustment_indexes)):
            raise ValueError("Dynamic Pricing demand adjustments must be aggregated per interval")

        if self.load_shift_entries:
            expected_total = sum(item.shifted_mw for item in self.load_shift_entries)
            if self.total_shifted_mw is None:
                raise ValueError("total_shifted_mw is required when load-shift entries exist")
            if abs(self.total_shifted_mw - expected_total) > tolerance:
                raise ValueError("total_shifted_mw must equal the sum of load-shift entries")

        if self.demand_adjustments:
            net_delta = sum(item.demand_delta_mw for item in self.demand_adjustments)
            if abs(net_delta) > tolerance:
                raise ValueError("Dynamic Pricing demand adjustments must conserve total demand")

            shifted_in = sum(max(item.demand_delta_mw, 0.0) for item in self.demand_adjustments)
            shifted_out = sum(max(-item.demand_delta_mw, 0.0) for item in self.demand_adjustments)
            if abs(shifted_in - shifted_out) > tolerance:
                raise ValueError("Dynamic Pricing shifted-in and shifted-out demand must match")
            if self.total_shifted_mw is not None and abs(self.total_shifted_mw - shifted_in) > tolerance:
                raise ValueError("total_shifted_mw must match conserved demand adjustments")

        if self.action_required is False:
            if self.load_shift_entries or self.demand_adjustments:
                raise ValueError("action_required=False cannot contain a load-shift action")
            if self.total_shifted_mw not in (None, 0.0):
                raise ValueError("action_required=False cannot report positive shifted load")

        return self

