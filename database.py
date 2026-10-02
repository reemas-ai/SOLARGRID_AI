from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def init_db(app):
    """Initialize database connection and create tables."""
    db.init_app(app)
    with app.app_context():
        db.create_all()


# ============================================================
# 1. AGENT RUN TRACE
# ============================================================
class AgentRunTrace(db.Model):
    __tablename__ = 'agent_run_traces'

    id = db.Column(db.Integer, primary_key=True)
    run_uuid = db.Column(db.String(64), unique=True, nullable=False, index=True)
    start_time = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    end_time = db.Column(db.DateTime, nullable=True)
    trigger_source = db.Column(db.String(100), nullable=False)  # e.g., 'SCHEDULED', 'EVENT_IMBALANCE', 'USER_PROMPT'
    tools_called = db.Column(db.JSON, nullable=True)  # Derived summary list
    decisions = db.Column(db.JSON, nullable=True)  # Reasoning steps
    status = db.Column(db.String(50), default='RUNNING')  # 'RUNNING', 'COMPLETED', 'FAILED'
    final_outcome_summary = db.Column(db.Text, nullable=True)

    # Relationships
    snapshots = db.relationship('SystemSnapshot', backref='agent_run', lazy=True)
    plans = db.relationship('Plan', backref='agent_run', lazy=True)
    rag_evidences = db.relationship('RAGEvidence', backref='agent_run', lazy=True)
    tool_call_logs = db.relationship('ToolCallLog', backref='agent_run', lazy=True,
                                      order_by='ToolCallLog.called_at')
    evaluation_runs = db.relationship('EvaluationRun', backref='agent_run', lazy=True)


# ============================================================
# 1b. TOOL CALL LOG (per-tool-call observability)
# ============================================================
class ToolCallLog(db.Model):
    """
    Fine-grained, queryable record of every individual tool invocation made
    during an agent run.
    """
    __tablename__ = 'tool_call_logs'

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('agent_run_traces.id'), nullable=False, index=True)
    step_index = db.Column(db.Integer, nullable=False)
    tool_name = db.Column(db.String(100), nullable=False, index=True)
    tool_category = db.Column(db.String(50), nullable=True)  # e.g., 'ANALYTICS', 'SIMULATION', 'RAG', 'DB', 'EXTERNAL_API'
    input_json = db.Column(db.JSON, nullable=True)
    output_json = db.Column(db.JSON, nullable=True)
    status = db.Column(db.String(50), default='SUCCESS')  # SUCCESS, PARTIAL, REJECTED, FAILED, TIMEOUT
    error_message = db.Column(db.Text, nullable=True)
    latency_ms = db.Column(db.Integer, nullable=True)
    called_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


# ============================================================
# 1c. EXTERNAL DATA PROVENANCE
# ============================================================
class ExternalDataProvenance(db.Model):
    """Durable provenance for public/open external data used by SolarGrid.

    External weather/reference data is deliberately separated from synthetic
    grid telemetry.  A row records where the data came from, freshness/quality,
    and which workflow consumed it without turning API failures into zeroes.
    """
    __tablename__ = 'external_data_provenance'

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(64), nullable=False, index=True)
    source_role = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(32), nullable=False, index=True)
    retrieved_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    observed_at = db.Column(db.DateTime, nullable=True)
    location = db.Column(db.String(180), nullable=True)
    variables_json = db.Column(db.JSON, nullable=True)
    payload_json = db.Column(db.JSON, nullable=True)
    endpoint = db.Column(db.String(500), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    cache_origin_id = db.Column(db.Integer, db.ForeignKey('external_data_provenance.id'), nullable=True)
    consumed_by = db.Column(db.String(120), nullable=True)
    consumed_run_id = db.Column(db.Integer, db.ForeignKey('agent_run_traces.id'), nullable=True, index=True)
    consumed_snapshot_id = db.Column(db.Integer, db.ForeignKey('system_snapshots.id'), nullable=True, index=True)


# ============================================================
# 1d. EVENT-DRIVEN AUTOMATION OBSERVABILITY
# ============================================================
class OperationalEvent(db.Model):
    __tablename__ = 'operational_events'

    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(100), nullable=False, index=True)
    trigger_source = db.Column(db.String(100), nullable=False, default='SCHEDULED_MONITOR')
    occurred_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey('system_snapshots.id'), nullable=True, index=True)
    run_id = db.Column(db.Integer, db.ForeignKey('agent_run_traces.id'), nullable=True, index=True)
    reason = db.Column(db.Text, nullable=True)
    severity = db.Column(db.String(50), nullable=True)
    state_signature = db.Column(db.String(128), nullable=True, index=True)
    status = db.Column(db.String(64), nullable=False, default='DETECTED')
    selected_plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=True)
    details_json = db.Column(db.JSON, nullable=True)


class MonitoringTick(db.Model):
    __tablename__ = 'monitoring_ticks'

    id = db.Column(db.Integer, primary_key=True)
    checked_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    run_id = db.Column(db.Integer, db.ForeignKey('agent_run_traces.id'), nullable=True, index=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey('system_snapshots.id'), nullable=True, index=True)
    condition = db.Column(db.String(100), nullable=True)
    event_type = db.Column(db.String(100), nullable=True)
    event_id = db.Column(db.Integer, db.ForeignKey('operational_events.id'), nullable=True)
    event_suppressed = db.Column(db.Boolean, default=False, nullable=False)
    external_source_ids = db.Column(db.JSON, nullable=True)
    details_json = db.Column(db.JSON, nullable=True)


# ============================================================
# 2. SYSTEM STATE / SNAPSHOTS
# ============================================================
class SystemSnapshot(db.Model):
    __tablename__ = 'system_snapshots'

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('agent_run_traces.id'), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    demand_mw = db.Column(db.Float, nullable=True)
    solar_gen_mw = db.Column(db.Float, nullable=True)
    other_gen_mw = db.Column(db.Float, default=0.0)
    battery_soc_pct = db.Column(db.Float, nullable=True)
    reserve_margin_mw = db.Column(db.Float, nullable=True)
    state_json = db.Column(db.JSON, nullable=True)  # Tool 01 state context
    grid_status = db.Column(db.String(50), default='STABLE')  # STABLE, WARNING, CRITICAL
    data_quality = db.Column(db.String(50), default='GOOD')
    data_source = db.Column(db.String(100), default='SIMULATION')
    is_synthetic = db.Column(db.Boolean, default=True, nullable=False)

    # Relationships
    forecasts = db.relationship('Forecast', backref='snapshot', lazy=True)
    imbalance_events = db.relationship('ImbalanceEvent', backref='snapshot', lazy=True)
    reserve_assessments = db.relationship('ReserveAssessment', backref='snapshot', lazy=True)
    scenarios = db.relationship('Scenario', backref='base_snapshot', lazy=True)


# ============================================================
# 3. FORECASTS & ACTUAL MEASUREMENTS
# ============================================================
class Forecast(db.Model):
    __tablename__ = 'forecasts'

    __table_args__ = (
        db.Index(
            'ix_forecasts_variable_target',
            'variable_name',
            'target_time'
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey('system_snapshots.id'), nullable=True)
    variable_name = db.Column(db.String(50), nullable=False)  # e.g. 'solar', 'demand'
    forecast_value = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(20), default='MW')
    source = db.Column(db.String(100), default='OPEN_METEO')
    issued_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    target_time = db.Column(db.DateTime, nullable=False, index=True)
    confidence_interval = db.Column(db.JSON, nullable=True)


class ActualMeasurement(db.Model):
    __tablename__ = 'actual_measurements'

    id = db.Column(db.Integer, primary_key=True)
    variable_name = db.Column(db.String(50), nullable=False)
    actual_value = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(20), default='MW')
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    source = db.Column(db.String(100), default='SIMULATION')
    is_synthetic = db.Column(db.Boolean, default=True, nullable=False)


class ForecastErrorLog(db.Model):
    __tablename__ = 'forecast_error_logs'

    id = db.Column(db.Integer, primary_key=True)
    forecast_id = db.Column(db.Integer, db.ForeignKey('forecasts.id'), nullable=False)
    actual_id = db.Column(db.Integer, db.ForeignKey('actual_measurements.id'), nullable=False)
    signed_error = db.Column(db.Float, nullable=False)  # forecast - actual
    abs_error = db.Column(db.Float, nullable=False)
    error_pct = db.Column(db.Float, nullable=True)
    error_direction = db.Column(db.String(20), nullable=False)  # OVERFORECAST, UNDERFORECAST, ACCURATE, UNKNOWN
    operational_impact = db.Column(db.Text, nullable=True)
    known_cause = db.Column(db.String(255), default='UNKNOWN', nullable=True)
    pattern_label = db.Column(db.String(100), nullable=True)

    forecast = db.relationship('Forecast', backref='error_log')
    actual = db.relationship('ActualMeasurement', backref='error_log')


# ============================================================
# 4. GRID ASSETS (Generators, Batteries, Loads, Network)
# ============================================================
class GridBus(db.Model):
    __tablename__ = 'grid_buses'

    id = db.Column(db.Integer, primary_key=True)
    bus_name = db.Column(db.String(100), nullable=False)
    nominal_kv = db.Column(db.Float, nullable=False)
    bus_type = db.Column(db.String(50), default='PQ')
    is_synthetic = db.Column(db.Boolean, default=True, nullable=False)

    generators = db.relationship('Generator', backref='bus', lazy=True)
    batteries = db.relationship('Battery', backref='bus', lazy=True)
    loads = db.relationship('Load', backref='bus', lazy=True)
    lines_from = db.relationship('GridLine', foreign_keys='GridLine.from_bus_id', backref='from_bus', lazy=True)
    lines_to = db.relationship('GridLine', foreign_keys='GridLine.to_bus_id', backref='to_bus', lazy=True)


class GridLine(db.Model):
    __tablename__ = 'grid_lines'

    id = db.Column(db.Integer, primary_key=True)
    line_name = db.Column(db.String(100), nullable=False)
    from_bus_id = db.Column(db.Integer, db.ForeignKey('grid_buses.id'), nullable=False)
    to_bus_id = db.Column(db.Integer, db.ForeignKey('grid_buses.id'), nullable=False)
    thermal_limit_mva = db.Column(db.Float, nullable=False)
    loading_pct = db.Column(db.Float, default=0.0)
    is_congested = db.Column(db.Boolean, default=False)
    is_synthetic = db.Column(db.Boolean, default=True, nullable=False)


class Generator(db.Model):
    __tablename__ = 'generators'

    id = db.Column(db.Integer, primary_key=True)
    bus_id = db.Column(db.Integer, db.ForeignKey('grid_buses.id'), nullable=True, index=True)
    name = db.Column(db.String(100), nullable=False)
    capacity_mw = db.Column(db.Float, nullable=False)
    current_output_mw = db.Column(db.Float, default=0.0)
    min_output_mw = db.Column(db.Float, default=0.0)
    max_output_mw = db.Column(db.Float, nullable=False)
    ramp_rate_mw_per_min = db.Column(db.Float, nullable=False)
    availability_status = db.Column(db.String(50), default='AVAILABLE')
    constraints_json = db.Column(db.JSON, nullable=True)
    is_synthetic = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Battery(db.Model):
    __tablename__ = 'batteries'

    id = db.Column(db.Integer, primary_key=True)
    bus_id = db.Column(db.Integer, db.ForeignKey('grid_buses.id'), nullable=True, index=True)
    name = db.Column(db.String(100), nullable=False)
    capacity_mwh = db.Column(db.Float, nullable=False)
    soc_pct = db.Column(db.Float, nullable=False)
    min_soc_pct = db.Column(db.Float, default=10.0)
    max_soc_pct = db.Column(db.Float, default=90.0)
    max_charge_mw = db.Column(db.Float, nullable=False)
    max_discharge_mw = db.Column(db.Float, nullable=False)
    efficiency = db.Column(db.Float, default=0.95)
    current_power_mw = db.Column(db.Float, default=0.0)
    availability_status = db.Column(db.String(50), default='AVAILABLE')
    is_synthetic = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Load(db.Model):
    __tablename__ = 'loads'

    id = db.Column(db.Integer, primary_key=True)
    bus_id = db.Column(db.Integer, db.ForeignKey('grid_buses.id'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    p_mw = db.Column(db.Float, nullable=False)
    q_mvar = db.Column(db.Float, default=0.0)
    load_type = db.Column(db.String(50), nullable=True)
    is_flexible = db.Column(db.Boolean, default=False)
    is_synthetic = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


# ============================================================
# 5. RESERVES & RISK / IMBALANCE EVENTS
# ============================================================
class ReserveAssessment(db.Model):
    __tablename__ = 'reserve_assessments'

    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey('system_snapshots.id'), nullable=False)
    required_reserve_mw = db.Column(db.Float, nullable=True)
    actual_reserve_mw = db.Column(db.Float, nullable=True)
    gen_contribution_mw = db.Column(db.Float, default=0.0)
    battery_contribution_mw = db.Column(db.Float, default=0.0)
    limiting_constraints = db.Column(db.JSON, nullable=True)
    time_horizon_hrs = db.Column(db.Integer, nullable=True)  # Deprecated
    time_horizon_minutes = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(50), default='UNKNOWN')  # SUFFICIENT, INSUFFICIENT, UNKNOWN


class ImbalanceEvent(db.Model):
    __tablename__ = 'imbalance_events'

    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey('system_snapshots.id'), nullable=False)
    event_type = db.Column(db.String(100), nullable=False)  # 'SOLAR_DROP', 'DEMAND_SPIKE', 'FUTURE_RISK'
    detection_time = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    target_time = db.Column(db.DateTime, nullable=True)
    severity = db.Column(db.String(20), default='MEDIUM')  # LOW, MEDIUM, HIGH, CRITICAL
    expected_imbalance_mw = db.Column(db.Float, nullable=False)
    risk_drivers = db.Column(db.JSON, nullable=True)
    status = db.Column(db.String(50), default='OPEN')  # OPEN, MITIGATED, CLOSED

    plans = db.relationship('Plan', backref='imbalance_event', lazy=True)


# ============================================================
# 6. PLANS & EVALUATIONS
# ============================================================
class Plan(db.Model):
    __tablename__ = 'plans'

    id = db.Column(db.Integer, primary_key=True)
    parent_plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=True, index=True)
    run_id = db.Column(db.Integer, db.ForeignKey('agent_run_traces.id'), nullable=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey('system_snapshots.id'), nullable=False)
    imbalance_event_id = db.Column(db.Integer, db.ForeignKey('imbalance_events.id'), nullable=True)
    candidate_id = db.Column(db.String(100), nullable=True)
    strategy_label = db.Column(db.String(100), nullable=True)
    optimization_status = db.Column(db.String(30), nullable=True)  # OPTIMAL, FEASIBLE, INFEASIBLE, UNKNOWN
    plan_name = db.Column(db.String(150), nullable=False)
    horizon_hours = db.Column(db.Integer, default=4)
    actions = db.Column(db.JSON, nullable=False)
    gen_dispatch_json = db.Column(db.JSON, nullable=True)
    battery_dispatch_json = db.Column(db.JSON, nullable=True)
    expected_reserve_mw = db.Column(db.Float, nullable=True)
    assumptions = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default='PROPOSED')  # PROPOSED, REJECTED, APPROVED, EXECUTED, STALE, SUPERSEDED

    evaluations = db.relationship('PlanEvaluation', backref='plan', lazy=True)
    approval = db.relationship('HumanApproval', backref='plan', uselist=False, lazy=True)
    execution = db.relationship('PlanExecution', backref='plan', uselist=False, lazy=True)
    rag_evidences = db.relationship('RAGEvidence', backref='plan', lazy=True)
    parent_plan = db.relationship('Plan', remote_side=[id], backref=db.backref('child_plans', lazy=True))


class PlanEvaluation(db.Model):
    __tablename__ = 'plan_evaluations'

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=False)
    is_feasible = db.Column(db.Boolean, nullable=True)  # True=feasible, False=infeasible, NULL=UNKNOWN
    constraint_violations = db.Column(db.JSON, nullable=True)
    power_flow_result = db.Column(db.JSON, nullable=True)
    reserve_margin_mw = db.Column(db.Float, nullable=True)
    imbalance_mw = db.Column(db.Float, nullable=True)
    estimated_cost = db.Column(db.Float, default=0.0)
    rejection_reason = db.Column(db.Text, nullable=True)
    decision_policy_version = db.Column(db.String(50), default='v1.0')


# ============================================================
# 7. SCENARIOS (WHAT-IF LAB)
# ============================================================
class Scenario(db.Model):
    __tablename__ = 'scenarios'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    base_snapshot_id = db.Column(db.Integer, db.ForeignKey('system_snapshots.id'), nullable=False)
    solar_delta_pct = db.Column(db.Float, default=0.0)
    demand_delta_pct = db.Column(db.Float, default=0.0)
    gen_outages_json = db.Column(db.JSON, nullable=True)
    simulation_results = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ============================================================
# 7b. EVALUATION RUNS (test/scenario coverage)
# ============================================================
class EvaluationRun(db.Model):
    """
    Records a single pass of the hackathon-required test set so the demo can
    show, straight from the DB, that normal / abnormal / replanning /
    failure cases were actually exercised and what happened.
    """
    __tablename__ = 'evaluation_runs'

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('agent_run_traces.id'), nullable=True)
    test_case_name = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(50), nullable=False)  # 'NORMAL', 'ABNORMAL', 'REPLANNING', 'FAILURE_WEAK_EVIDENCE'
    description = db.Column(db.Text, nullable=True)
    expected_behavior = db.Column(db.Text, nullable=False)
    actual_behavior = db.Column(db.Text, nullable=True)
    passed = db.Column(db.Boolean, nullable=True)  # null until the case has been executed/graded
    executed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    notes = db.Column(db.Text, nullable=True)


# ============================================================
# 8. MONITORING, APPROVALS, EXECUTION & OUTCOMES
# ============================================================
class MonitoringLog(db.Model):
    __tablename__ = 'monitoring_logs'

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    trigger_type = db.Column(db.String(100), nullable=False)
    old_state = db.Column(db.Text, nullable=True)
    new_state = db.Column(db.Text, nullable=True)
    magnitude_of_change = db.Column(db.Float, nullable=True)
    threshold_crossed = db.Column(db.String(100), nullable=True)
    affected_plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=True)
    action_taken = db.Column(db.Text, nullable=True)


class HumanApproval(db.Model):
    __tablename__ = 'human_approvals'

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=False)
    status = db.Column(db.String(50), default='PENDING')  # PENDING, APPROVED, REJECTED
    reviewer_comment = db.Column(db.Text, nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    plan_version = db.Column(db.Integer, default=1)


class PlanExecution(db.Model):
    __tablename__ = 'plan_executions'

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=False)
    execution_timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    requested_actions = db.Column(db.JSON, nullable=False)
    actual_actions = db.Column(db.JSON, nullable=True)
    status = db.Column(db.String(50), default='SUCCESS')  # SUCCESS, PARTIAL_FAIL, FAILED
    deviations = db.Column(db.JSON, nullable=True)
    failure_info = db.Column(db.Text, nullable=True)

    outcome = db.relationship('OperationalOutcome', backref='execution', uselist=False, lazy=True)


class OperationalOutcome(db.Model):
    __tablename__ = 'operational_outcomes'

    id = db.Column(db.Integer, primary_key=True)
    execution_id = db.Column(db.Integer, db.ForeignKey('plan_executions.id'), nullable=False)
    expected_outcome = db.Column(db.Text, nullable=False)
    actual_outcome = db.Column(db.Text, nullable=False)
    is_success = db.Column(db.Boolean, nullable=True)
    causes = db.Column(db.Text, nullable=True)
    operational_impact = db.Column(db.Text, nullable=True)
    planning_implications = db.Column(db.Text, nullable=True)

    lessons = db.relationship('LessonMemory', backref='outcome', lazy=True)


# ============================================================
# 9. RAG EVIDENCE & LESSONS / MEMORY
# ============================================================
class RAGEvidence(db.Model):
    __tablename__ = 'rag_evidences'

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=True)
    run_id = db.Column(db.Integer, db.ForeignKey('agent_run_traces.id'), nullable=True)
    document_source = db.Column(db.String(255), nullable=False)  # e.g., NEPCO Grid Code PDF
    source_type = db.Column(db.String(50), default='GRID_CODE')
    doc_metadata = db.Column(db.JSON, nullable=True)
    chunk_text = db.Column(db.Text, nullable=False)
    query_used = db.Column(db.Text, nullable=False)
    retrieval_timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    is_synthetic = db.Column(db.Boolean, default=False, nullable=False)


class LessonMemory(db.Model):
    __tablename__ = 'lesson_memories'

    id = db.Column(db.Integer, primary_key=True)
    source_outcome_id = db.Column(db.Integer, db.ForeignKey('operational_outcomes.id'), nullable=True)
    source_forecast_error_id = db.Column(db.Integer, db.ForeignKey('forecast_error_logs.id'), nullable=True)
    observed_pattern = db.Column(db.Text, nullable=False)
    evidence_summary = db.Column(db.Text, nullable=False)
    frequency_count = db.Column(db.Integer, default=1)
    confidence_score = db.Column(db.Float, nullable=True)
    conditions = db.Column(db.JSON, nullable=True)
    operational_impact = db.Column(db.Text, nullable=True)
    planning_implication = db.Column(db.Text, nullable=False)
    valid_until = db.Column(db.DateTime, nullable=True)

    forecast_error = db.relationship('ForecastErrorLog', backref='lessons', lazy=True)


# ============================================================
# 10. CHANGE IMPACT ASSESSMENT (New - Tool 12)
# ============================================================
class ChangeImpactAssessment(db.Model):
    __tablename__ = 'change_impact_assessments'

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('agent_run_traces.id'), nullable=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=False, index=True)
    monitoring_log_id = db.Column(db.Integer, db.ForeignKey('monitoring_logs.id'), nullable=True)
    current_snapshot_id = db.Column(db.Integer, db.ForeignKey('system_snapshots.id'), nullable=False)

    impact_level = db.Column(db.String(20), nullable=False)  # NONE, LOW, MEDIUM, HIGH, CRITICAL, UNKNOWN
    still_valid = db.Column(db.Boolean, nullable=True)
    replan_required = db.Column(db.Boolean, nullable=False)

    affected_assumptions = db.Column(db.JSON, nullable=True)
    affected_actions = db.Column(db.JSON, nullable=True)
    operational_effect = db.Column(db.JSON, nullable=True)
    supporting_metrics = db.Column(db.JSON, nullable=True)
    reason = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    run = db.relationship('AgentRunTrace', backref='change_impact_assessments', lazy=True)
    plan = db.relationship('Plan', backref='change_impact_assessments', lazy=True)
    monitoring_log = db.relationship('MonitoringLog', backref='change_impact_assessments', lazy=True)
    current_snapshot = db.relationship('SystemSnapshot', backref='change_impact_assessments', lazy=True)