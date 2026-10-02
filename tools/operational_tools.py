"""SOLARGRID AI operational tools 10-16.

All demo thresholds/policies are configuration, never hidden in tool logic.
Tools are deterministic and simulation-only for this course project.
"""
from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from contextvars import ContextVar, Token
from contextlib import contextmanager

from database import (
    db, AgentRunTrace, SystemSnapshot, Plan, Scenario, MonitoringLog,
    ChangeImpactAssessment, HumanApproval, PlanExecution,
    OperationalOutcome, Forecast, ActualMeasurement, ReserveAssessment,
    ForecastErrorLog, LessonMemory, RAGEvidence, ToolCallLog,
)
from config_loader import load_decision_policy, load_dynamic_pricing_config, load_json
from plan_lifecycle import transition_plan
from runtime import next_step_index
from tool_common import resolve_dynamic_pricing_context_ref
import simulation
from tools.state_tools import calculate_reserve
from RAG.rag_engine import get_engine

ROOT = Path(__file__).resolve().parents[1]

# Dispatcher binds the active AgentRunTrace id for the duration of one
# operational tool call. ContextVar keeps this request-scoped and avoids
# changing every internal _result(...) call signature.
_ACTIVE_RUN_ID: ContextVar[int | None] = ContextVar("solargrid_active_run_id", default=None)

@contextmanager
def bind_run_id(run_id: int | None):
    token: Token = _ACTIVE_RUN_ID.set(run_id)
    try:
        yield
    finally:
        _ACTIVE_RUN_ID.reset(token)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_goal_verification_policy() -> dict[str, Any] | None:
    """Load the approved delayed goal-verification timing configuration."""
    policy = load_decision_policy() or {}
    verification = policy.get("goal_verification")
    if not isinstance(verification, dict):
        return None
    delay = verification.get("verification_delay_minutes")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0:
        return None
    if float(delay) != int(delay):
        return None
    return {"verification_delay_minutes": int(delay)}


def _verification_due_at(execution_timestamp: datetime, policy: dict[str, Any]) -> datetime:
    return execution_timestamp + timedelta(minutes=policy["verification_delay_minutes"])


def _plan_horizon_minutes(plan: Plan) -> float:
    """Resolve the deterministic planning horizon represented by a persisted Plan."""
    actions = plan.actions if isinstance(plan.actions, dict) else {}
    intervals = actions.get("intervals") or []
    interval_minutes = actions.get("interval_minutes")
    try:
        if interval_minutes is not None and intervals:
            value = float(interval_minutes) * len(intervals)
            if value > 0:
                return value
    except (TypeError, ValueError):
        pass
    if plan.horizon_hours is not None:
        try:
            value = float(plan.horizon_hours) * 60.0
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 120.0


def _latest_reserve_assessment(snapshot_id: int, horizon_minutes: float | None = None) -> ReserveAssessment | None:
    rows = (
        ReserveAssessment.query
        .filter_by(snapshot_id=snapshot_id)
        .order_by(ReserveAssessment.id.desc())
        .all()
    )
    if horizon_minutes is None:
        return rows[0] if rows else None
    for row in rows:
        if row.time_horizon_minutes is None:
            continue
        if abs(float(row.time_horizon_minutes) - float(horizon_minutes)) <= 1e-9:
            return row
    return None


def _ensure_post_execution_reserve(snapshot_id: int, plan: Plan) -> tuple[ReserveAssessment | None, dict[str, Any]]:
    """Ensure post-execution reserve is recomputed from the post snapshot itself.

    Tool 14 must not compare post-execution conditions with a reserve number that
    belonged to the pre-execution planning snapshot. The post snapshot has its
    own asset state and its own follow-up solar/demand forecast window, so Tool
    02's deterministic reserve calculation is run against that evidence.
    """
    horizon_minutes = _plan_horizon_minutes(plan)
    existing = _latest_reserve_assessment(snapshot_id, horizon_minutes)
    if (
        existing is not None
        and existing.required_reserve_mw is not None
        and existing.actual_reserve_mw is not None
    ):
        snapshot = db.session.get(SystemSnapshot, snapshot_id)
        if snapshot is not None:
            snapshot.reserve_margin_mw = float(existing.actual_reserve_mw) - float(existing.required_reserve_mw)
            db.session.commit()
        return existing, {
            "tool_status": "SUCCESS",
            "domain_status": "OK",
            "data": {
                "assessment_id": existing.id,
                "snapshot_id": snapshot_id,
                "required_reserve_mw": existing.required_reserve_mw,
                "actual_reserve_mw": existing.actual_reserve_mw,
                "reserve_margin_mw": (
                    None if existing.actual_reserve_mw is None or existing.required_reserve_mw is None
                    else float(existing.actual_reserve_mw) - float(existing.required_reserve_mw)
                ),
                "reserve_status": existing.status,
            },
            "message": "Existing post-execution reserve assessment reused.",
        }

    result = calculate_reserve(
        db.session,
        snapshot_id,
        horizon_minutes=horizon_minutes,
    )
    data = result.get("data") if isinstance(result, dict) else None
    assessment_id = data.get("assessment_id") if isinstance(data, dict) else None
    assessment = db.session.get(ReserveAssessment, int(assessment_id)) if assessment_id is not None else None
    return assessment, result if isinstance(result, dict) else {
        "tool_status": "FAILED",
        "domain_status": "UNKNOWN",
        "data": None,
        "message": "Post-execution reserve assessment returned an invalid result.",
    }


def _load_thresholds(profile: str | dict[str, Any] | None = None) -> dict[str, Any] | None:
    if isinstance(profile, dict):
        return profile
    cfg = load_json("thresholds.json")
    if profile in (None, "DEMO_DEFAULT"):
        return cfg
    if cfg.get("profile_name") == profile or cfg.get("profile_version") == profile:
        return cfg
    return None


def _tool_log(name: str, status: str, input_data: dict[str, Any], output: dict[str, Any], run_id: int | None = None):
    if run_id is None:
        return
    row = ToolCallLog(
        run_id=run_id,
        step_index=next_step_index(AgentRunTrace, ToolCallLog, run_id),
        tool_name=name,
        tool_category="OPERATIONAL",
        input_json=input_data,
        output_json=output,
        status=status,
    )
    db.session.add(row)


def _result(status: str, data: Any, message: str, *, run_id: int | None = None,
            tool_name: str | None = None, input_data: dict[str, Any] | None = None):
    effective_run_id = run_id if run_id is not None else _ACTIVE_RUN_ID.get()
    out = {"status": status, "data": data, "message": message}
    if effective_run_id is not None and tool_name:
        _tool_log(tool_name, status, input_data or {}, out, effective_run_id)
        db.session.commit()
    return out


def run_scenario_analysis(base_snapshot_id: int, horizon_minutes: int = 60,
                          solar_delta_pct: float | None = 0.0,
                          demand_delta_pct: float | None = 0.0,
                          generator_outages: list[int | str] | None = None,
                          other_changes: dict[str, Any] | None = None,
                          horizon_hours: float | None = None):
    """Run isolated deterministic what-if analysis; never mutates base state."""
    # Boundary validation: reject non-finite horizons/deltas rather than allowing
    # NaN/Infinity to propagate into the simulated state. These are invalid inputs,
    # not physical UNKNOWN values.
    try:
        if horizon_hours is not None:
            horizon_hours_value = float(horizon_hours)
            if not math.isfinite(horizon_hours_value):
                return _result("REJECTED", None, "horizon_hours must be finite.", tool_name="run_scenario_analysis", input_data=locals())
            horizon_minutes = int(round(horizon_hours_value * 60))
        horizon_minutes_value = float(horizon_minutes)
    except (TypeError, ValueError, OverflowError):
        return _result("REJECTED", None, "horizon_minutes must be a finite number.", tool_name="run_scenario_analysis", input_data=locals())
    if not math.isfinite(horizon_minutes_value) or horizon_minutes_value <= 0:
        return _result("REJECTED", None, "horizon_minutes must be positive and finite.", tool_name="run_scenario_analysis", input_data=locals())
    horizon_minutes = int(horizon_minutes_value)

    if solar_delta_pct is not None:
        try:
            solar_delta_pct = float(solar_delta_pct)
        except (TypeError, ValueError):
            return _result("REJECTED", None, "solar_delta_pct must be numeric or null.", tool_name="run_scenario_analysis", input_data=locals())
        if not math.isfinite(solar_delta_pct):
            return _result("REJECTED", None, "solar_delta_pct must be finite or null.", tool_name="run_scenario_analysis", input_data=locals())
        if solar_delta_pct < -100.0:
            return _result("REJECTED", None, "solar_delta_pct cannot reduce solar generation below zero.", tool_name="run_scenario_analysis", input_data=locals())
    if demand_delta_pct is not None:
        try:
            demand_delta_pct = float(demand_delta_pct)
        except (TypeError, ValueError):
            return _result("REJECTED", None, "demand_delta_pct must be numeric or null.", tool_name="run_scenario_analysis", input_data=locals())
        if not math.isfinite(demand_delta_pct):
            return _result("REJECTED", None, "demand_delta_pct must be finite or null.", tool_name="run_scenario_analysis", input_data=locals())
        if demand_delta_pct < -100.0:
            return _result("REJECTED", None, "demand_delta_pct cannot reduce demand below zero.", tool_name="run_scenario_analysis", input_data=locals())

    base = db.session.get(SystemSnapshot, base_snapshot_id)
    if not base:
        return _result("ERROR", None, f"Base snapshot with ID {base_snapshot_id} not found.", tool_name="run_scenario_analysis", input_data=locals())

    state = deepcopy(base.state_json) if isinstance(base.state_json, dict) else {}
    state.setdefault("solar_generation_mw", base.solar_gen_mw)
    state.setdefault("demand_mw", base.demand_mw)
    state.setdefault("generators", [])

    outages = [str(x) for x in (generator_outages or [])]
    resulting = simulation.apply_scenario_to_state(
        state,
        solar_delta_pct=solar_delta_pct,
        demand_delta_pct=demand_delta_pct,
        generator_outages=outages,
        other_changes=other_changes,
    )

    base_solar = base.solar_gen_mw
    base_demand = base.demand_mw
    new_solar = resulting.get("solar_generation_mw")
    new_demand = resulting.get("demand_mw")
    base_net = None if base_solar is None or base_demand is None else base_solar - base_demand
    new_net = None if new_solar is None or new_demand is None else new_solar - new_demand

    policy = load_decision_policy()
    reserve = {
        "status": "UNKNOWN",
        "required_mw": None,
        "reason": "Scenario reserve requires a complete forecast series for the configured reserve formula.",
    }
    if new_demand is not None:
        rr = policy.get("required_reserve", {})
        reserve_fraction = rr.get("reserve_fraction")
        solar_fraction = rr.get("solar_variability_fraction")
        # The approved policy explicitly requires the maximum absolute solar
        # forecast change. Tool 10 does not invent that series from a single
        # scenario delta. If a forecast series is present in the snapshot, use it.
        forecast = resulting.get("forecast")
        solar_values = []
        if isinstance(forecast, dict):
            candidate = forecast.get("solar")
            if isinstance(candidate, list):
                solar_values = [float(v) for v in candidate if isinstance(v, (int, float)) and math.isfinite(float(v))]
        elif isinstance(forecast, list):
            for point in forecast:
                if isinstance(point, dict) and isinstance(point.get("solar"), (int, float)) and math.isfinite(float(point["solar"])):
                    solar_values.append(float(point["solar"]))
        if reserve_fraction is not None and solar_fraction is not None and len(solar_values) >= 2:
            max_abs_forecast_change_mw = max(
                abs(b - a) for a, b in zip(solar_values, solar_values[1:])
            )
            reserve["required_mw"] = (
                float(reserve_fraction) * float(new_demand)
                + float(solar_fraction) * max_abs_forecast_change_mw
            )
            reserve["status"] = "ESTIMATED_FROM_APPROVED_DEMO_POLICY"
            reserve["max_abs_forecast_change_mw"] = max_abs_forecast_change_mw

    risk = {"status": "UNKNOWN", "reason": "Scenario risk is UNKNOWN unless complete future evidence is available."}
    tolerance = policy.get("hard_constraints", {}).get("residual_imbalance_tolerance_mw")
    if new_net is not None and tolerance is not None:
        abs_imb = abs(new_net)
        risk = {
            "status": "ELEVATED" if abs_imb > float(tolerance) else "WITHIN_TOLERANCE",
            "residual_imbalance_mw": new_net,
            "tolerance_mw": tolerance,
        }

    results = {
        "base_snapshot_id": base_snapshot_id,
        "horizon_minutes": horizon_minutes,
        "changed_assumptions": {
            "solar_delta_pct": solar_delta_pct,
            "demand_delta_pct": demand_delta_pct,
            "generator_outages": outages,
            "other_changes": other_changes or {},
        },
        "resulting_state": resulting,
        "risk": risk,
        "reserve": reserve,
        "plan_results": [],
        "constraint_results": {"status": "NOT_EVALUATED", "reason": "No replacement plan is selected by Tool 10."},
        "power_flow": {"status": "NOT_EVALUATED", "reason": "Tool 10 does not replace Tool 08."},
        "comparison_to_base": {
            "solar_delta_mw": None if base_solar is None or new_solar is None else new_solar - base_solar,
            "demand_delta_mw": None if base_demand is None or new_demand is None else new_demand - base_demand,
            "net_balance_delta_mw": None if base_net is None or new_net is None else new_net - base_net,
        },
    }
    scenario = Scenario(
        name=f"Scenario_Snap_{base_snapshot_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        base_snapshot_id=base_snapshot_id,
        solar_delta_pct=solar_delta_pct or 0.0,
        demand_delta_pct=demand_delta_pct or 0.0,
        gen_outages_json=outages,
        simulation_results=results,
    )
    db.session.add(scenario)
    db.session.commit()
    results["scenario_id"] = scenario.id
    return _result("SUCCESS", results, "Scenario analysis completed without mutating the base snapshot.", tool_name="run_scenario_analysis", input_data=locals())


def monitor_system_conditions(active_plan_id: int, current_snapshot_id: int,
                              threshold_profile: str | dict[str, Any] | None = None,
                              threshold_config_path: str | None = None):
    """Detect configured material changes for an active plan.

    Forecast changes are read from Forecast rows linked to the baseline/current
    snapshots when available. Missing forecast evidence remains UNKNOWN and is
    never replaced with a zero or inferred value.
    """
    #new
    audit_input = {
        "active_plan_id": active_plan_id,
        "current_snapshot_id": current_snapshot_id,
        "threshold_profile": threshold_profile,
        "threshold_config_path": threshold_config_path,
    }
    #new
    plan = db.session.get(Plan, active_plan_id)
    current = db.session.get(SystemSnapshot, current_snapshot_id)
    if not plan or not current:
        
        return _result(
            "ERROR",
            None,
            "Active plan or current snapshot not found.",
            tool_name="monitor_system_conditions",
            input_data=audit_input,
                )
        #return _result("ERROR", None, "Active plan or current snapshot not found.", tool_name="monitor_system_conditions", input_data=locals())

    profile = _load_thresholds(threshold_profile)
    if threshold_config_path:
        with open(threshold_config_path, "r", encoding="utf-8") as fh:
            profile = json.load(fh)
    if not profile:
        #new
        return _result(
            "REJECTED",
            None,
            "No approved threshold profile is available.",
            tool_name="monitor_system_conditions",
            input_data=audit_input,
            )

        #return _result("REJECTED", None, "No approved threshold profile is available.", tool_name="monitor_system_conditions", input_data=locals())

    base = db.session.get(SystemSnapshot, plan.snapshot_id)
    if not base:
        #new
        return _result(
            "ERROR",
            None,
            "Plan baseline snapshot not found.",
            tool_name="monitor_system_conditions",
            input_data=audit_input,
            )           
        #return _result("ERROR", None, "Plan baseline snapshot not found.", tool_name="monitor_system_conditions", input_data=locals())

    cfg = profile.get("tool11_monitoring", profile)
    cumulative_cfg = cfg.get("cumulative", {})
    changes = []

    def add_change(variable, old, new, magnitude, threshold):
        crossed = None if threshold is None or magnitude is None else magnitude > float(threshold)
        changes.append({
            "variable": variable, "old_value": old, "new_value": new,
            "magnitude": magnitude, "threshold_crossed": crossed,
        })
        return crossed

    def forecast_records(snapshot_id: int, variable: str):
        rows = Forecast.query.filter_by(snapshot_id=snapshot_id).filter(
            Forecast.variable_name.in_([variable, f"{variable}_mw"])
        ).order_by(Forecast.target_time.asc(), Forecast.id.asc()).all()
        return rows

    def compare_forecasts(variable: str, threshold: float | None):
        old_rows = forecast_records(base.id, variable)
        new_rows = forecast_records(current.id, variable)
        old_map = {(r.target_time, r.unit): r for r in old_rows if r.forecast_value is not None}
        new_map = {(r.target_time, r.unit): r for r in new_rows if r.forecast_value is not None}
        matched = []
        for key, old_row in old_map.items():
            new_row = new_map.get(key)
            if new_row is None:
                continue
            old_value = float(old_row.forecast_value)
            new_value = float(new_row.forecast_value)
            if not (math.isfinite(old_value) and math.isfinite(new_value)):
                continue
            magnitude = None if old_value == 0 else abs((new_value - old_value) / old_value) * 100
            matched.append((old_row, new_row, old_value, new_value, magnitude))
        if matched:
            # A forecast series is material if any matched target time crosses the configured threshold.
            selected = max(matched, key=lambda x: (-1 if x[4] is None else x[4]))
            old_row, new_row, old_value, new_value, magnitude = selected
            crossed = any(m[4] is not None and threshold is not None and m[4] > float(threshold) for m in matched)
            changes.append({
                "variable": f"{variable}_forecast",
                "old_value": old_value,
                "new_value": new_value,
                "magnitude": magnitude,
                "threshold_crossed": crossed,
                "target_time": new_row.target_time.isoformat() if new_row.target_time else None,
                "unit": new_row.unit,
                "source": new_row.source,
            })
            return crossed

        # Fallback only when the snapshot explicitly contains forecast evidence.
        base_forecast = base_state.get("forecast", {}) if isinstance(base_state, dict) else {}
        current_forecast = current_state.get("forecast", {}) if isinstance(current_state, dict) else {}
        old_value = base_forecast.get(variable) if isinstance(base_forecast, dict) else None
        new_value = current_forecast.get(variable) if isinstance(current_forecast, dict) else None
        if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)) and math.isfinite(float(old_value)) and math.isfinite(float(new_value)):
            old_value = float(old_value); new_value = float(new_value)
            magnitude = None if old_value == 0 else abs((new_value-old_value)/old_value)*100
            return add_change(f"{variable}_forecast", old_value, new_value, magnitude, threshold)
        # A partial forecast record is insufficient to establish a change.
        # Preserve the UNKNOWN condition at the domain-evidence level rather
        # than emitting a misleading change event with a null threshold flag.
        return None

    base_state = base.state_json if isinstance(base.state_json, dict) else {}
    current_state = current.state_json if isinstance(current.state_json, dict) else {}

    compare_forecasts("solar", cfg.get("solar_forecast_change_pct"))
    compare_forecasts("demand", cfg.get("demand_forecast_change_pct"))
    forecast_evidence_unknown = not any(c.get("variable") in {"solar_forecast", "demand_forecast"} for c in changes)

    add_change("battery_soc_pct", base.battery_soc_pct, current.battery_soc_pct,
               None if base.battery_soc_pct is None or current.battery_soc_pct is None else abs(current.battery_soc_pct-base.battery_soc_pct),
               cfg.get("battery_soc_change_pct_points"))

    gen_changed = base_state.get("generators") != current_state.get("generators") if "generators" in base_state and "generators" in current_state else None
    if gen_changed is True:
        changes.append({"variable": "generator_availability", "old_value": base_state.get("generators"), "new_value": current_state.get("generators"), "magnitude": None, "threshold_crossed": True})

    forecast_changed = base_state.get("forecast") != current_state.get("forecast") if "forecast" in base_state and "forecast" in current_state else None
    if forecast_changed is True and not any(c["variable"] in {"solar_forecast", "demand_forecast"} for c in changes):
        changes.append({"variable": "forecast_revision", "old_value": base_state.get("forecast"), "new_value": current_state.get("forecast"), "magnitude": None, "threshold_crossed": True})

    # The approved profile currently exposes only a normalized score threshold,
    # not the normalization/aggregation formula itself. Do not invent one.
    # Cumulative sub-threshold behavior is therefore left UNKNOWN until the
    # profile defines that formula explicitly.
    cumulative_cross = False

    crossed = any(c.get("threshold_crossed") is True for c in changes)
    numeric = [c["magnitude"] for c in changes if isinstance(c.get("magnitude"), (int, float)) and math.isfinite(float(c["magnitude"]))]
    normalized_score = None if not numeric else sum(numeric)
    trigger_reason = next((c["variable"] for c in changes if c.get("threshold_crossed") is True), None)
    log = MonitoringLog(
        timestamp=_now_utc(), trigger_type="SYSTEM_MONITORING",
        old_state=json.dumps(base_state), new_state=json.dumps(current_state),
        magnitude_of_change=normalized_score, threshold_crossed=trigger_reason,
        affected_plan_id=active_plan_id, action_taken="LOGGED_CHANGE" if crossed else "NO_ACTION",
    )
    db.session.add(log); db.session.commit()
    #new edit here 
    same_snapshot = int(plan.snapshot_id) == int(current_snapshot_id)
    return _result("SUCCESS", {
        "monitoring_event_id": log.id, "change_detected": crossed, "changes": changes,
        "affected_plan_id": active_plan_id, "trigger_reason": trigger_reason,
        "forecast_evidence_status": "UNKNOWN" if forecast_evidence_unknown else "AVAILABLE",
        "evaluation_status": "WAITING_FOR_TELEMETRY" if same_snapshot else "EVALUATED",
        "fresh_telemetry": "WAITING" if same_snapshot else "AVAILABLE",
        "comparison_context": {
            "baseline_snapshot_id": plan.snapshot_id,
            "current_snapshot_id": current_snapshot_id,
            "same_snapshot": same_snapshot,
            "observed_change_available": not same_snapshot,
            "message": (
                "No newer grid snapshot is available; approval alone does not change the physical grid."
                if same_snapshot
                else "Monitoring compared the approved-plan baseline with a newer snapshot."
            ),
        },
    }, "System conditions monitored using the configured demo threshold profile.", tool_name="monitor_system_conditions",  input_data=audit_input)


def assess_change_impact(plan_id: int, current_snapshot_id: int, monitoring_log_id: int | None = None):
    """Revalidate the unchanged plan against a new snapshot/context.

    Tool 12 owns the impact decision. Tools 06/07/08 remain independently
    implemented validators; an optional context_snapshot_id was added to their
    shared request schema so Tool 12 can ask them to validate the same Plan
    against the current state without mutating Plan.snapshot_id.
    """
    plan = db.session.get(Plan, plan_id)
    current = db.session.get(SystemSnapshot, current_snapshot_id)
    audit_input = {
        "plan_id": plan_id,
        "current_snapshot_id": current_snapshot_id,
        "monitoring_log_id": monitoring_log_id,
    }
    if not plan or not current:
        return _result("ERROR", None, "Plan or current snapshot not found.", tool_name="assess_change_impact", input_data=audit_input)

    log = db.session.get(MonitoringLog, monitoring_log_id) if monitoring_log_id else None
    if monitoring_log_id and not log:
        return _result("ERROR", None, "Monitoring event not found.", tool_name="assess_change_impact", input_data=audit_input)

    thresholds = _load_thresholds("DEMO_DEFAULT") or {}
    monitoring_cfg = thresholds.get("tool11_monitoring", {}) or {}
    impact_cfg = thresholds.get("tool12_change_impact", {}) or {}
    policy = load_decision_policy() or {}

    base = db.session.get(SystemSnapshot, plan.snapshot_id)
    evidence: list[dict[str, Any]] = []
    validation: dict[str, Any] = {}

    # Snapshot changes are supporting evidence only. Resource feasibility is
    # determined by the independent validators below.
    if base is not None:
        pairs = [
            ("solar_generation", base.solar_gen_mw, current.solar_gen_mw, monitoring_cfg.get("solar_forecast_change_pct")),
            ("demand", base.demand_mw, current.demand_mw, monitoring_cfg.get("demand_forecast_change_pct")),
            ("battery_soc", base.battery_soc_pct, current.battery_soc_pct, monitoring_cfg.get("battery_soc_change_pct_points")),
        ]
        for variable, old, new, threshold in pairs:
            if old is None or new is None:
                evidence.append({"variable": variable, "status": "UNKNOWN", "old_value": old, "new_value": new, "threshold": threshold})
                continue
            magnitude = abs(new - old) if variable == "battery_soc" else (None if old == 0 else abs((new - old) / old) * 100.0)
            if magnitude is None:
                evidence.append({"variable": variable, "status": "UNKNOWN", "old_value": old, "new_value": new, "threshold": threshold})
                continue
            evidence.append({"variable": variable, "status": "AVAILABLE", "old_value": old, "new_value": new, "magnitude": magnitude, "threshold": threshold,
                             "threshold_crossed": threshold is not None and magnitude > float(threshold)})

    # Forecast revisions are explicit evidence. Only comparable MW forecast
    # pairs are used; missing pairs remain UNKNOWN and are never coerced.
    def _forecast_map(snapshot_id: int):
        rows = Forecast.query.filter_by(snapshot_id=snapshot_id).all()
        out = {}
        for row in rows:
            if row.variable_name not in {"solar", "demand"} or row.unit != "MW" or row.forecast_value is None:
                continue
            key = (row.variable_name, row.unit, row.target_time)
            current_row = out.get(key)
            if current_row is None or (row.issued_at and current_row.issued_at and row.issued_at > current_row.issued_at):
                out[key] = row
        return out

    base_forecasts = _forecast_map(plan.snapshot_id) if base is not None else {}
    current_forecasts = _forecast_map(current_snapshot_id)
    forecast_keys = sorted(set(base_forecasts) | set(current_forecasts), key=lambda x: (x[0], x[2]))
    for key in forecast_keys:
        old_row, new_row = base_forecasts.get(key), current_forecasts.get(key)
        if old_row is None or new_row is None:
            evidence.append({"variable": f"{key[0]}_forecast", "status": "UNKNOWN", "target_time": key[2].isoformat(),
                             "old_value": old_row.forecast_value if old_row else None,
                             "new_value": new_row.forecast_value if new_row else None,
                             "unit": key[1], "reason": "Comparable forecast pair is incomplete."})
            continue
        old_val, new_val = float(old_row.forecast_value), float(new_row.forecast_value)
        magnitude = abs(new_val - old_val) if old_val == 0 else abs((new_val - old_val) / old_val) * 100.0
        threshold_key = "solar_forecast_change_pct" if key[0] == "solar" else "demand_forecast_change_pct"
        threshold = monitoring_cfg.get(threshold_key)
        evidence.append({"variable": f"{key[0]}_forecast", "status": "AVAILABLE", "target_time": key[2].isoformat(),
                         "old_value": old_val, "new_value": new_val, "unit": key[1], "magnitude": magnitude,
                         "threshold": threshold, "threshold_crossed": threshold is not None and magnitude > float(threshold)})

    if current.grid_status is None:
        evidence.append({"variable": "grid_status", "status": "UNKNOWN"})
    else:
        evidence.append({"variable": "grid_status", "status": "AVAILABLE", "value": current.grid_status,
                         "threshold_crossed": str(current.grid_status).upper() == "CRITICAL"})
    if current.reserve_margin_mw is None:
        evidence.append({"variable": "reserve_margin_mw", "status": "UNKNOWN", "value": None})
    else:
        evidence.append({"variable": "reserve_margin_mw", "status": "AVAILABLE", "value": current.reserve_margin_mw,
                         "threshold_crossed": current.reserve_margin_mw < 0.0})

    # Resolve the explicit Tool 12 interface gap: validators accept an optional
    # context_snapshot_id. The Plan row itself is never mutated.
    from tools.planning_tools import check_generator_constraints, check_battery_constraints, run_power_flow
    models = {
        "Plan": Plan,
        "Generator": __import__("database").Generator,
        "Battery": __import__("database").Battery,
        "SystemSnapshot": SystemSnapshot,
        "AgentRunTrace": __import__("database").AgentRunTrace,
        "ToolCallLog": ToolCallLog,
    }
    try:
        validation["generator"] = check_generator_constraints({"plan_id": plan_id, "context_snapshot_id": current_snapshot_id}, db=db, models=models, run_id=plan.run_id)
        validation["battery"] = check_battery_constraints({"plan_id": plan_id, "context_snapshot_id": current_snapshot_id}, db=db, models=models, run_id=plan.run_id)

        network_required = bool((policy.get("network_validation_required") is True))
        network_id = policy.get("network_id")
        if network_required or network_id:
            validation["network"] = run_power_flow({"plan_id": plan_id, "network_id": network_id,
                                                      "context_snapshot_id": current_snapshot_id}, db=db, models=models, run_id=plan.run_id)
    except Exception as exc:
        db.session.rollback()
        validation["validation_error"] = str(exc)

    gen = validation.get("generator") or {}
    bat = validation.get("battery") or {}
    pf = validation.get("network")
    validator_infeasible = gen.get("aggregate_feasible") is False or bat.get("aggregate_feasible") is False
    validator_unknown = gen.get("aggregate_feasible") is None or bat.get("aggregate_feasible") is None
    network_infeasible = pf is not None and pf.get("is_network_feasible") is False
    network_unknown = pf is not None and pf.get("is_network_feasible") is None

    crossed = any(e.get("threshold_crossed") is True for e in evidence)
    critical = any(e.get("variable") == "grid_status" and e.get("threshold_crossed") is True for e in evidence)
    critical = critical or network_infeasible

    if critical:
        level = "CRITICAL"
        still_valid = False
        replan = True
    elif validator_infeasible:
        level = "HIGH"
        still_valid = False
        replan = True
    elif crossed:
        # An explicit configured material-change threshold is deterministic
        # evidence of impact. UNKNOWN validation evidence must not erase a
        # separately observed threshold crossing; it only limits the
        # validator-derived evidence.
        level = "HIGH" if impact_cfg.get("high_if_crossed_count", 1) <= 1 else "MEDIUM"
        still_valid = False if level == "HIGH" else True
        replan = level == "HIGH"
    elif validator_unknown or network_unknown or any(e.get("status") == "UNKNOWN" for e in evidence):
        level = "UNKNOWN"
        still_valid = None
        replan = False
    else:
        any_change = any(e.get("magnitude", 0) not in (None, 0, 0.0) for e in evidence)
        level = "LOW" if any_change else "NONE"
        still_valid = True
        replan = False

    reasons = []
    for e in evidence:
        if e.get("threshold_crossed") is True:
            reasons.append(str(e.get("variable")))
    if validator_infeasible:
        reasons.append("independent resource validation infeasible")
    if validator_unknown or network_unknown:
        reasons.append("insufficient validation evidence")
    reason = "; ".join(dict.fromkeys(reasons)) or "No material invalidating change detected."

    # Keep observed grid changes separate from the dispatch changes the approved
    # plan *would* make. Approval itself does not alter the physical grid, so
    # comparing a plan against the exact same snapshot is expected to produce
    # 0% observed change. Expose that context explicitly and provide first-
    # interval planned deltas for the operator UI instead of implying that
    # approval already changed the system.
    same_snapshot = int(plan.snapshot_id) == int(current_snapshot_id)
    planned_action_deltas: list[dict[str, Any]] = []
    actions = plan.actions if isinstance(plan.actions, dict) else {}
    intervals = actions.get("intervals") if isinstance(actions, dict) else None
    first_interval = intervals[0] if isinstance(intervals, list) and intervals else {}

    current_generators = {
        str(item.get("id")): item
        for item in ((current.state_json or {}).get("generators") or [])
        if item.get("id") is not None
    }
    current_batteries = {
        str(item.get("id")): item
        for item in ((current.state_json or {}).get("batteries") or [])
        if item.get("id") is not None
    }

    for resource_id, point in ((first_interval or {}).get("generators") or {}).items():
        if not isinstance(point, dict):
            continue
        before = (current_generators.get(str(resource_id)) or {}).get("current_output_mw")
        target = point.get("output_mw")
        if before is None or target is None:
            delta_mw = None
            delta_pct = None
        else:
            delta_mw = float(target) - float(before)
            delta_pct = None if abs(float(before)) < 1e-12 else (delta_mw / abs(float(before))) * 100.0
        planned_action_deltas.append({
            "resource_type": "generator",
            "resource_id": str(resource_id),
            "variable": f"generator_{resource_id}_dispatch",
            "old_value": before,
            "new_value": target,
            "delta_mw": delta_mw,
            "magnitude": None if delta_pct is None else abs(delta_pct),
            "change_pct": delta_pct,
        })

    for resource_id, point in ((first_interval or {}).get("batteries") or {}).items():
        if not isinstance(point, dict):
            continue
        before = (current_batteries.get(str(resource_id)) or {}).get("current_power_mw")
        target = point.get("power_mw")
        if before is None or target is None:
            delta_mw = None
            delta_pct = None
        else:
            delta_mw = float(target) - float(before)
            delta_pct = None if abs(float(before)) < 1e-12 else (delta_mw / abs(float(before))) * 100.0
        planned_action_deltas.append({
            "resource_type": "battery",
            "resource_id": str(resource_id),
            "variable": f"battery_{resource_id}_dispatch",
            "old_value": before,
            "new_value": target,
            "delta_mw": delta_mw,
            "magnitude": None if delta_pct is None else abs(delta_pct),
            "change_pct": delta_pct,
        })

    affected_assumptions = evidence + [{"validation": validation}]
    operational_effect = {"grid_status": current.grid_status, "reserve_margin_mw": current.reserve_margin_mw}
    supporting_metrics = {
        "solar_gen_mw": current.solar_gen_mw,
        "demand_mw": current.demand_mw,
        "battery_soc_pct": current.battery_soc_pct,
        "generator_validation": gen.get("aggregate_feasible"),
        "battery_validation": bat.get("aggregate_feasible"),
        "network_validation": pf.get("is_network_feasible") if pf else None,
    }

    assessment = ChangeImpactAssessment(
        run_id=plan.run_id, plan_id=plan.id, monitoring_log_id=log.id if log else None,
        current_snapshot_id=current_snapshot_id, impact_level=level,
        still_valid=still_valid, replan_required=replan,
        affected_assumptions=affected_assumptions, affected_actions=plan.actions,
        operational_effect=operational_effect, supporting_metrics=supporting_metrics, reason=reason,
    )
    db.session.add(assessment)
    db.session.commit()

    return _result("SUCCESS", {
        "plan_id": plan_id, "still_valid": still_valid, "impact_level": level,
        "affected_assumptions": affected_assumptions, "affected_actions": plan.actions,
        "operational_effect": operational_effect, "replan_required": replan,
        "reason": reason, "supporting_metrics": supporting_metrics,
        "assessment_id": assessment.id,
        "validation": validation,
        "comparison_context": {
            "baseline_snapshot_id": plan.snapshot_id,
            "current_snapshot_id": current_snapshot_id,
            "same_snapshot": same_snapshot,
            "observed_change_available": not same_snapshot,
            "message": (
                "No newer grid snapshot is available; observed operating changes are therefore zero by design."
                if same_snapshot
                else "Observed operating changes were compared against a newer snapshot."
            ),
        },
        "planned_action_deltas": planned_action_deltas,
    }, "Change impact assessed against the current snapshot using independent validators and configured evidence.", tool_name="assess_change_impact", input_data=audit_input)


def execute_and_verify_plan(plan_id: int, approval_id: int, execution_mode: str = "SIMULATION"):
    plan = db.session.get(Plan, plan_id)
    run_id = plan.run_id if plan is not None else None

    # Tool-call audit input must remain JSON-serializable. Never pass ORM objects
    # such as Plan/HumanApproval through locals() into a JSON column.
    audit_input = {
        "plan_id": plan_id,
        "approval_id": approval_id,
        "execution_mode": execution_mode,
    }

    if execution_mode != "SIMULATION":
        return _result(
            "REJECTED", None, "Only SIMULATION execution is permitted.",
            run_id=run_id, tool_name="execute_and_verify_plan", input_data=audit_input
        )

    approval = db.session.get(HumanApproval, approval_id)
    if not plan or not approval or approval.plan_id != plan_id or approval.status != "APPROVED":
        return _result(
            "REJECTED", None, "Exact approved HumanApproval for the exact plan is required.",
            run_id=run_id, tool_name="execute_and_verify_plan", input_data=audit_input
        )

    if plan.status != "APPROVED":
        return _result(
            "REJECTED", None, f"Plan status {plan.status!r} is not APPROVED.",
            run_id=run_id, tool_name="execute_and_verify_plan", input_data=audit_input
        )

    blockers = ChangeImpactAssessment.query.filter_by(plan_id=plan_id, replan_required=True).all()
    newer = [b for b in blockers if approval.reviewed_at is None or b.created_at > approval.reviewed_at]
    if newer:
        return _result(
            "REJECTED", None, "A newer ChangeImpactAssessment requires replanning.",
            run_id=run_id, tool_name="execute_and_verify_plan", input_data=audit_input
        )

    if not hasattr(simulation, "apply_plan"):
        return _result(
            "REJECTED", None, "Simulation executor is not configured.",
            run_id=run_id, tool_name="execute_and_verify_plan", input_data=audit_input
        )
    verification_policy = _load_goal_verification_policy()
    if verification_policy is None:
        return _result(
            "REJECTED", None,
            "No valid goal-verification delay is configured.",
            run_id=run_id, tool_name="execute_and_verify_plan", input_data=audit_input,
        )

    sim = simulation.apply_plan(plan_id)
    exec_status = sim.get("execution_status", "FAILED")

    actions = plan.actions if isinstance(plan.actions, dict) else {}
    has_dynamic_pricing = actions.get("dynamic_pricing_context") is not None
    dynamic_pricing_execution = sim.get("dynamic_pricing_execution")
    if has_dynamic_pricing and exec_status != "FAILED":
        if not isinstance(dynamic_pricing_execution, dict) or not dynamic_pricing_execution.get("conservation_verified"):
            exec_status = "FAILED"
            sim["failure_information"] = (
                "Dynamic Pricing execution evidence is missing or failed conservation verification."
            )

    # A successful/partial physical execution creates a new authoritative
    # post-execution snapshot. Recalculate reserve for that snapshot before
    # Tool 14 evaluates the operational goal; otherwise reserve evidence would
    # remain None and the UI would correctly but unhelpfully show NOT EVALUATED.
    post_reserve_assessment = None
    post_reserve_result: dict[str, Any] = {
        "tool_status": "NOT_RUN",
        "domain_status": "UNKNOWN",
        "data": None,
        "message": "No post-execution snapshot was returned.",
    }
    post_snapshot_id = sim.get("post_execution_snapshot_id")
    if post_snapshot_id is not None:
        try:
            post_reserve_assessment, post_reserve_result = _ensure_post_execution_reserve(
                int(post_snapshot_id), plan
            )
        except Exception as exc:
            # Do not rewrite a real execution result merely because the
            # follow-up assessment failed. Expose the missing evidence clearly
            # so Tool 14 can remain UNKNOWN rather than fabricate a PASS/FAIL.
            db.session.rollback()
            post_reserve_result = {
                "tool_status": "FAILED",
                "domain_status": "UNKNOWN",
                "data": None,
                "message": f"Post-execution reserve assessment failed: {exc}",
            }

    execution = PlanExecution(
        plan_id=plan_id, execution_timestamp=_now_utc(),
        requested_actions=sim.get("requested_actions", plan.actions or {}),
        actual_actions=sim.get("actual_actions"), status=exec_status,
        deviations=sim.get("deviations"), failure_info=sim.get("failure_information"),
    )
    db.session.add(execution)
    if exec_status == "SUCCESS":
        transition_plan(plan, "EXECUTED")
    db.session.commit()
    response = "SUCCESS"
    verification_due_at = _verification_due_at(execution.execution_timestamp, verification_policy)
    return _result(response, {
        "execution_id": execution.id, "plan_id": plan_id,
        "requested_actions": execution.requested_actions, "actual_actions": execution.actual_actions,
        # Public Tool 13 contract: expose the execution result explicitly.
        "execution_status": exec_status,
        "deviations": execution.deviations,
        "failure_info": execution.failure_info, "timestamp": execution.execution_timestamp.isoformat() + "Z",
        "verification_delay_minutes": verification_policy["verification_delay_minutes"],
        "verification_due_at": verification_due_at.isoformat() + "Z",
        "pre_execution_snapshot_id": sim.get("pre_execution_snapshot_id"),
        "post_execution_snapshot_id": sim.get("post_execution_snapshot_id"),
        "post_execution_reserve_assessment_id": (
            post_reserve_assessment.id if post_reserve_assessment is not None else None
        ),
        "post_execution_reserve": post_reserve_result,
        "followup_forecast_ids": sim.get("followup_forecast_ids", []),
        "followup_forecast_source": sim.get("followup_forecast_source"),
        "simulation_time": sim.get("simulation_time"),
        "dynamic_pricing_execution": dynamic_pricing_execution,
    }, "Plan execution completed in simulation; delayed goal verification is scheduled by configuration.", run_id=run_id, tool_name="execute_and_verify_plan", input_data=audit_input)


def _command_tracking_tolerance() -> tuple[float, float]:
    """Return (accepted tolerance MW, exact-equality epsilon MW) from policy."""
    policy = load_decision_policy() or {}
    cfg = policy.get("command_tracking") or {}
    try:
        tolerance = max(0.0, float(cfg.get("tolerance_mw", 1e-6)))
    except (TypeError, ValueError):
        tolerance = 1e-6
    try:
        exact_epsilon = max(0.0, float(cfg.get("exact_epsilon_mw", 1e-6)))
    except (TypeError, ValueError):
        exact_epsilon = 1e-6
    exact_epsilon = min(exact_epsilon, tolerance) if tolerance > 0 else 0.0
    return tolerance, exact_epsilon


def compare_requested_actual_actions(requested_actions, actual_actions):
    rows = _expected_actual_rows(requested_actions, actual_actions)
    if not rows:
        return None, []
    mismatches = [
        {"asset_type": row["asset_type"], "id": row["asset_id"], "interval": row["interval"],
         "requested_mw": row["expected_mw"], "actual_mw": row["actual_mw"]}
        for row in rows if row["match"] is not True
    ]
    return len(mismatches) == 0, mismatches


def execution_status_requires_replan(status: str | None) -> bool:
    return str(status or "").upper() in {"FAILED", "PARTIAL_FAIL"}


def _determine_goal_result(*, execution_success: bool, balance_ok: bool | None,
                           reserve_ok: bool | None) -> tuple[str, dict[str, bool | None]]:
    """Deterministically classify operational goal achievement from evidence.

    Execution status and operational goal achievement are intentionally separate
    concepts.  A PARTIAL_FAIL/FAILED command attempt must not automatically turn
    an otherwise measurable post-execution grid outcome into NOT_EVALUATED.
    Tool 14 therefore evaluates the goal whenever balance/reserve evidence exists;
    execution_success is preserved elsewhere for command/execution reporting and
    replanning decisions.
    """
    criteria = {
        "balance": balance_ok,
        "reserve": reserve_ok,
    }

    evaluated = [value for value in criteria.values() if value is not None]
    if not evaluated:
        return "NOT_EVALUATED", criteria
    # Residual power balance is the primary operational objective.
    # A reserve-only pass cannot convert a failed balance objective into
    # PARTIAL success; PARTIAL is reserved for a satisfied balance objective
    # with an unmet reserve objective.
    if balance_ok is False:
        return "NOT_ACHIEVED", criteria
    if balance_ok is True and reserve_ok is True:
        return "ACHIEVED", criteria
    if balance_ok is True and reserve_ok is False:
        return "PARTIAL", criteria
    return "NOT_EVALUATED", criteria


def _snapshot_operator_metrics(snapshot: SystemSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {"demand_mw": None, "total_generation_mw": None, "battery_power_mw": None,
                "reserve_margin_mw": None, "residual_imbalance_mw": None, "battery_soc_pct": None}
    total_generation = None
    if snapshot.solar_gen_mw is not None and snapshot.other_gen_mw is not None:
        total_generation = float(snapshot.solar_gen_mw) + float(snapshot.other_gen_mw)
    battery_power = None
    state = snapshot.state_json if isinstance(snapshot.state_json, dict) else {}
    batteries = state.get("batteries") or []
    powers = [b.get("current_power_mw") for b in batteries if isinstance(b, dict) and isinstance(b.get("current_power_mw"), (int, float))]
    if powers:
        battery_power = sum(float(v) for v in powers)
    residual = None
    if total_generation is not None and snapshot.demand_mw is not None:
        # Positive battery power means discharge and contributes to meeting load.
        residual = total_generation + float(battery_power or 0.0) - float(snapshot.demand_mw)
    return {
        "demand_mw": snapshot.demand_mw,
        "total_generation_mw": total_generation,
        "battery_power_mw": battery_power,
        "reserve_margin_mw": snapshot.reserve_margin_mw,
        "residual_imbalance_mw": residual,
        "battery_soc_pct": snapshot.battery_soc_pct,
    }


def _expected_actual_rows(requested_actions: Any, actual_actions: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(requested_actions, dict) or not isinstance(actual_actions, dict):
        return rows
    tolerance, exact_epsilon = _command_tracking_tolerance()
    req_intervals = requested_actions.get("intervals") or []
    act_intervals = actual_actions.get("intervals") or []
    actual_by_index = {str(x.get("index")): x for x in act_intervals if isinstance(x, dict)}

    def comparison(expected, actual):
        if expected is None or actual is None:
            return None, None, None
        delta = float(actual) - float(expected)
        abs_delta = abs(delta)
        if abs_delta <= exact_epsilon:
            state = "EXACT"
        elif abs_delta <= tolerance:
            state = "WITHIN_TOLERANCE"
        else:
            state = "OUT_OF_TOLERANCE"
        return state != "OUT_OF_TOLERANCE", delta, state

    for req in req_intervals:
        if not isinstance(req, dict):
            continue
        idx = str(req.get("index"))
        act = actual_by_index.get(idx, {})
        for kind, expected_key in (("generators", "output_mw"), ("batteries", "power_mw")):
            expected_assets = req.get(kind) or {}
            actual_assets = {str(x.get("id")): x.get("actual_mw") for x in (act.get(kind) or []) if isinstance(x, dict)}
            for asset_id, point in expected_assets.items():
                expected = point.get(expected_key) if isinstance(point, dict) else None
                actual = actual_assets.get(str(asset_id))
                match, delta, state = comparison(expected, actual)
                rows.append({
                    "interval": req.get("index"),
                    "asset_type": ("generator" if kind == "generators" else "battery"),
                    "asset_id": str(asset_id),
                    "expected_mw": expected,
                    "actual_mw": actual,
                    "delta_mw": delta,
                    "tolerance_mw": tolerance,
                    "comparison": state,
                    "match": match,
                })

        # Solar curtailment is part of the dispatch command too. Track it as a
        # first-class expected-vs-actual action so a future curtailment execution
        # deviation cannot be hidden behind generator/battery matches.
        expected_curtailment = req.get("solar_curtailment_mw")
        actual_curtailment = act.get("solar_curtailment_mw") if isinstance(act, dict) else None
        if expected_curtailment is not None or actual_curtailment is not None:
            match, delta, state = comparison(expected_curtailment, actual_curtailment)
            rows.append({
                "interval": req.get("index"),
                "asset_type": "solar_curtailment",
                "asset_id": "solar",
                "expected_mw": expected_curtailment,
                "actual_mw": actual_curtailment,
                "delta_mw": delta,
                "tolerance_mw": tolerance,
                "comparison": state,
                "match": match,
            })
    return rows


def _dynamic_pricing_outcome_for_tool14(plan: Plan, execution: PlanExecution):
    """Resolve Tool 17 provenance and evaluate DP expected-vs-actual impact."""
    from schemas import PlanDispatch
    from tools.dynamic_pricing_tools import evaluate_dynamic_pricing_outcome

    actions = plan.actions if isinstance(plan.actions, dict) else {}
    dispatch = PlanDispatch.model_validate(actions)
    if dispatch.dynamic_pricing_context is None:
        return None

    base_snapshot = db.session.get(SystemSnapshot, plan.snapshot_id)
    if base_snapshot is None:
        raise ValueError("Dynamic Pricing Tool 14 verification requires the plan base snapshot")
    horizon_minutes = int(dispatch.interval_minutes) * len(dispatch.intervals)
    result = resolve_dynamic_pricing_context_ref(
        dispatch.dynamic_pricing_context,
        expected_snapshot_id=plan.snapshot_id,
        expected_horizon_minutes=horizon_minutes,
        db=db,
        ToolCallLog=ToolCallLog,
        snapshot=base_snapshot,
        interval_minutes=dispatch.interval_minutes,
        consumer_name="Tool 14 assess_and_diagnose_outcome",
    )
    cfg = load_dynamic_pricing_config(result.config_version)
    verification = cfg.get("outcome_verification") or {}
    tolerance = verification.get("deviation_tolerance_ratio")
    try:
        tolerance = float(tolerance)
    except (TypeError, ValueError):
        raise ValueError("Dynamic Pricing outcome deviation tolerance is not configured")
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("Dynamic Pricing outcome deviation tolerance must be finite and non-negative")
    return evaluate_dynamic_pricing_outcome(
        result,
        execution.actual_actions,
        deviation_tolerance_ratio=tolerance,
    )


def assess_and_diagnose_outcome(execution_id: int, pre_execution_snapshot_id: int | None = None,
                                post_execution_snapshot_id: int | None = None,
                                balance_tolerance_mw: float | None = None):
    """Assess execution and operational outcome from persisted evidence.

    The tool deliberately keeps three questions separate:
      1) execution_success: did simulation report successful execution?
      2) command_match: did actual actions match requested actions?
      3) operational_success: did the available post-execution evidence establish
         that the operational objective was achieved?

    Missing operational evidence remains UNKNOWN (None); it is never converted
    to success or failure merely because commands matched.
    """
    execution = db.session.get(PlanExecution, execution_id)
    if not execution:
        return _result(
            "ERROR", None, "Plan execution not found.",
            tool_name="assess_and_diagnose_outcome", input_data={"execution_id": execution_id}
        )

    plan = db.session.get(Plan, execution.plan_id)
    if not plan:
        return _result(
            "ERROR", None, "Associated plan not found.",
            tool_name="assess_and_diagnose_outcome", input_data={"execution_id": execution_id}
        )

    pre_snapshot = None
    if pre_execution_snapshot_id is not None:
        pre_snapshot = db.session.get(SystemSnapshot, pre_execution_snapshot_id)
        if pre_snapshot is None:
            return _result(
                "ERROR", None, "Pre-execution snapshot not found.",
                tool_name="assess_and_diagnose_outcome",
                input_data={"execution_id": execution_id, "pre_execution_snapshot_id": pre_execution_snapshot_id,
                            "post_execution_snapshot_id": post_execution_snapshot_id},
            )

    verification_policy = _load_goal_verification_policy()
    if verification_policy is None:
        return _result(
            "REJECTED", None,
            "No valid goal-verification delay is configured.",
            tool_name="assess_and_diagnose_outcome",
            input_data={"execution_id": execution_id, "post_execution_snapshot_id": post_execution_snapshot_id},
        )
    verification_due_at = _verification_due_at(execution.execution_timestamp, verification_policy)
    now = _now_utc()

    # In simulation the world clock may advance through the complete planning
    # horizon in one request. If Tool 13 provides an explicit authoritative
    # post-execution snapshot whose timestamp is already at/after the configured
    # verification due time, that snapshot satisfies the delay contract even
    # when wall-clock time has not advanced by the same amount. For real/live
    # flows without such a snapshot, the original wall-clock delay still applies.
    explicit_due_snapshot = None
    if post_execution_snapshot_id is not None:
        explicit_due_snapshot = db.session.get(SystemSnapshot, post_execution_snapshot_id)
        if explicit_due_snapshot is None:
            return _result(
                "ERROR", None, "Post-execution snapshot not found.",
                tool_name="assess_and_diagnose_outcome",
                input_data={"execution_id": execution_id, "post_execution_snapshot_id": post_execution_snapshot_id}
            )

    explicit_snapshot_is_due = bool(
        explicit_due_snapshot is not None
        and explicit_due_snapshot.timestamp is not None
        and explicit_due_snapshot.timestamp >= verification_due_at
    )

    if now < verification_due_at and not explicit_snapshot_is_due:
        return _result(
            "PENDING",
            {
                "execution_id": execution_id,
                "verification_status": "NOT_DUE",
                "verification_delay_minutes": verification_policy["verification_delay_minutes"],
                "verification_due_at": verification_due_at.isoformat() + "Z",
                "checked_at": now.isoformat() + "Z",
            },
            "Delayed goal verification is not due yet; no operational conclusion was recorded.",
            tool_name="assess_and_diagnose_outcome",
            input_data={"execution_id": execution_id, "post_execution_snapshot_id": post_execution_snapshot_id},
        )

    policy = load_decision_policy() or {}
    hard = policy.get("hard_constraints") or {}
    configured_tolerance = hard.get("residual_imbalance_tolerance_mw")
    tolerance = configured_tolerance if balance_tolerance_mw is None else balance_tolerance_mw
    if tolerance is None:
        return _result(
            "REJECTED", None,
            "No approved balance tolerance is available.",
            tool_name="assess_and_diagnose_outcome",
            input_data={"execution_id": execution_id, "post_execution_snapshot_id": post_execution_snapshot_id}
        )

    # An explicit snapshot id is authoritative. Without one, the current schema
    # has no execution_id FK on SystemSnapshot, so timestamp matching is only a
    # fallback and is reported as lower-confidence evidence.
    snapshot = None
    snapshot_resolution = "NONE"
    if post_execution_snapshot_id is not None:
        snapshot = explicit_due_snapshot
        if snapshot.timestamp < verification_due_at:
            return _result(
                "REJECTED", None,
                "The supplied snapshot predates the configured goal-verification due time.",
                tool_name="assess_and_diagnose_outcome",
                input_data={"execution_id": execution_id, "post_execution_snapshot_id": post_execution_snapshot_id}
            )
        snapshot_resolution = "EXPLICIT_ID"
    elif execution.execution_timestamp is not None:
        # Resolve the latest authoritative snapshot first, then validate its
        # timestamp against the verification due time.  This is intentionally
        # done as a two-step decision rather than embedding the datetime
        # comparison in the ORM filter: SystemSnapshot timestamps are persisted
        # as naive UTC DateTime values, and the authoritative rule is simply
        # "use the newest snapshot only if it was captured at/after due time".
        # If the newest snapshot is still too old, no older snapshot can be
        # valid, so verification must remain unevaluated.
        latest_snapshot = (
            SystemSnapshot.query
            .order_by(SystemSnapshot.timestamp.desc())
            .first()
        )
        if latest_snapshot is not None and latest_snapshot.timestamp >= verification_due_at:
            snapshot = latest_snapshot
            snapshot_resolution = "LATEST_DUE_SNAPSHOT"

    command_match, mismatches = compare_requested_actual_actions(
        execution.requested_actions, execution.actual_actions
    )
    execution_success = execution.status == "SUCCESS"

    # Operational evidence is intentionally limited to values actually present
    # in the persisted post-execution snapshot. No missing value is replaced by 0.
    balance_ok = None
    imbalance_mw = None
    if snapshot is not None:
        required = (snapshot.demand_mw, snapshot.solar_gen_mw, snapshot.other_gen_mw)
        if all(value is not None for value in required):
            state = snapshot.state_json if isinstance(snapshot.state_json, dict) else {}
            batteries = state.get("batteries") or []
            battery_values = [
                b.get("current_power_mw") for b in batteries
                if isinstance(b, dict)
            ]
            if all(value is not None for value in battery_values):
                battery_power_mw = sum(float(value) for value in battery_values) if battery_values else 0.0
                imbalance_mw = (
                    snapshot.solar_gen_mw
                    + snapshot.other_gen_mw
                    + battery_power_mw
                    - snapshot.demand_mw
                )
                balance_ok = abs(imbalance_mw) <= float(tolerance)

    # Reserve verification is based on a horizon-matched ReserveAssessment for
    # the *post-execution* snapshot. plan.expected_reserve_mw belongs to the
    # planning context and is not a valid threshold for a later snapshot.
    reserve_assessment = None
    reserve_assessment_status = "NOT_AVAILABLE"
    reserve_required_mw = None
    reserve_actual_mw = None
    reserve_margin_mw = snapshot.reserve_margin_mw if snapshot is not None else None
    reserve_ok = None
    if snapshot is not None:
        reserve_horizon_minutes = _plan_horizon_minutes(plan)
        reserve_assessment = _latest_reserve_assessment(snapshot.id, reserve_horizon_minutes)
        if (
            reserve_assessment is None
            or reserve_assessment.required_reserve_mw is None
            or reserve_assessment.actual_reserve_mw is None
        ):
            try:
                reserve_assessment, reserve_refresh = _ensure_post_execution_reserve(snapshot.id, plan)
                reserve_assessment_status = str(
                    (reserve_refresh.get("data") or {}).get("reserve_status")
                    or reserve_refresh.get("domain_status")
                    or reserve_refresh.get("tool_status")
                    or "UNKNOWN"
                ).upper()
            except Exception:
                db.session.rollback()
                reserve_assessment = None
        if reserve_assessment is not None:
            reserve_required_mw = reserve_assessment.required_reserve_mw
            reserve_actual_mw = reserve_assessment.actual_reserve_mw
            reserve_assessment_status = str(reserve_assessment.status or "UNKNOWN").upper()
            if reserve_required_mw is not None and reserve_actual_mw is not None:
                reserve_margin_mw = float(reserve_actual_mw) - float(reserve_required_mw)
                snapshot.reserve_margin_mw = reserve_margin_mw
                reserve_ok = float(reserve_actual_mw) + 1e-9 >= float(reserve_required_mw)
                db.session.flush()

    # Preserve the existing operational-success contract: execution success,
    # command matching when determinable, and measurable operational checks are
    # separate from the new goal-achievement classification.
    operational_checks = [value for value in (balance_ok, reserve_ok) if value is not None]
    required_checks = list(operational_checks)
    if command_match is not None:
        required_checks.insert(0, command_match)
    if not execution_success:
        operational_success = False
    elif required_checks:
        operational_success = all(required_checks) if operational_checks else None
    else:
        operational_success = None

    goal_result, goal_criteria = _determine_goal_result(
        execution_success=execution_success,
        balance_ok=balance_ok,
        reserve_ok=reserve_ok,
    )

    try:
        dynamic_pricing_outcome_model = _dynamic_pricing_outcome_for_tool14(plan, execution)
    except Exception as exc:
        db.session.rollback()
        return _result(
            "REJECTED", None,
            f"Dynamic Pricing outcome evidence could not be verified: {exc}",
            tool_name="assess_and_diagnose_outcome",
            input_data={"execution_id": execution_id, "post_execution_snapshot_id": post_execution_snapshot_id},
        )
    dynamic_pricing_outcome = (
        dynamic_pricing_outcome_model.model_dump(mode="json")
        if dynamic_pricing_outcome_model is not None else None
    )

    # Causes must be evidence-backed. Simulation may provide explicit failure
    # information or per-deviation reasons; otherwise preserve UNKNOWN.
    causes = []
    if execution.failure_info:
        causes.append(str(execution.failure_info))
    elif isinstance(execution.deviations, dict):
        for items in execution.deviations.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("reason"):
                    causes.append(str(item["reason"]))
    elif isinstance(execution.deviations, list):
        for item in execution.deviations:
            if isinstance(item, dict) and item.get("reason"):
                causes.append(str(item["reason"]))
    causes = list(dict.fromkeys(causes)) or ["UNKNOWN"]

    deviations = list(mismatches)
    if imbalance_mw is not None and balance_ok is False:
        deviations.append({
            "type": "POWER_BALANCE",
            "imbalance_mw": imbalance_mw,
            "tolerance_mw": float(tolerance),
        })
    if reserve_ok is False:
        deviations.append({
            "type": "RESERVE_SHORTFALL",
            "required_reserve_mw": reserve_required_mw,
            "actual_reserve_mw": reserve_actual_mw,
            "reserve_margin_mw": reserve_margin_mw,
            "reserve_assessment_id": reserve_assessment.id if reserve_assessment is not None else None,
        })
    if dynamic_pricing_outcome is not None and dynamic_pricing_outcome.get("significant_deviation") is True:
        deviations.append({
            "type": "DYNAMIC_PRICING_IMPACT_DEVIATION",
            "tool_call_id": dynamic_pricing_outcome.get("tool_call_id"),
            "goal_result": dynamic_pricing_outcome.get("goal_result"),
            "deviations": dynamic_pricing_outcome.get("deviations"),
            "deviation_tolerance_ratio": dynamic_pricing_outcome.get("deviation_tolerance_ratio"),
            "lesson_code": dynamic_pricing_outcome.get("lesson_code"),
        })

    operational_impact = list(deviations)
    planning_implications = []
    if goal_result == "NOT_ACHIEVED":
        planning_implications.append("Executed strategy did not achieve the measurable operational objective.")
    elif goal_result == "PARTIAL":
        planning_implications.append("Executed strategy achieved only part of the measurable operational objective.")
    elif goal_result == "NOT_EVALUATED":
        planning_implications.append("Goal achievement could not be evaluated from sufficient post-execution evidence.")
    if dynamic_pricing_outcome is not None:
        if dynamic_pricing_outcome.get("lesson_signal"):
            planning_implications.append(str(dynamic_pricing_outcome["lesson_signal"]))
        elif dynamic_pricing_outcome.get("goal_result") == "NOT_EVALUATED":
            planning_implications.append("Dynamic Pricing impact could not be fully evaluated from persisted execution evidence.")

    # Evidence quality is descriptive, not a score. The exact grading policy is
    # not defined by the specification, so do not invent numerical thresholds.
    if execution.failure_info or any(
        isinstance(item, dict) and item.get("reason")
        for item in (execution.deviations or [])
        if isinstance(execution.deviations, list)
    ):
        evidence_quality = "HIGH"
    elif snapshot is not None and operational_checks:
        evidence_quality = "HIGH" if snapshot_resolution == "EXPLICIT_ID" else "MEDIUM"
    elif snapshot is not None or command_match is not None:
        evidence_quality = "LOW"
    else:
        evidence_quality = "UNKNOWN"

    expected = {
        "plan_id": plan.id,
        "requested_actions": execution.requested_actions,
        "goal_definition": {
            "residual_imbalance_tolerance_mw": float(tolerance),
            "required_reserve_mw": float(reserve_required_mw) if reserve_required_mw is not None else None,
        },
        "dynamic_pricing": (
            {
                key: value for key, value in dynamic_pricing_outcome.items()
                if key.startswith("expected_") or key in {"tool_call_id", "deviation_tolerance_ratio"}
            } if dynamic_pricing_outcome is not None else None
        ),
    }
    actual = {
        "status": execution.status,
        "actual_actions": execution.actual_actions,
        "snapshot_id": snapshot.id if snapshot else None,
        "snapshot_resolution": snapshot_resolution,
        "verification_delay_minutes": verification_policy["verification_delay_minutes"],
        "verification_due_at": verification_due_at.isoformat() + "Z",
        "verification_checked_at": now.isoformat() + "Z",
        "goal_result": goal_result,
        "goal_criteria": goal_criteria,
        "observed": {
            "imbalance_mw": imbalance_mw,
            "reserve_margin_mw": reserve_margin_mw,
            "required_reserve_mw": reserve_required_mw,
            "actual_reserve_mw": reserve_actual_mw,
            "reserve_assessment_id": reserve_assessment.id if reserve_assessment is not None else None,
            "reserve_status": reserve_assessment_status,
        },
        "dynamic_pricing": dynamic_pricing_outcome,
    }

    before_metrics = _snapshot_operator_metrics(pre_snapshot)
    after_metrics = _snapshot_operator_metrics(snapshot)
    before_after = {
        "pre_execution_snapshot_id": pre_snapshot.id if pre_snapshot else None,
        "post_execution_snapshot_id": snapshot.id if snapshot else None,
        "metrics": [
            {"key": key, "before": before_metrics.get(key), "after": after_metrics.get(key)}
            for key in ("demand_mw", "total_generation_mw", "battery_power_mw", "reserve_margin_mw", "residual_imbalance_mw", "battery_soc_pct")
        ],
    }
    expected_vs_actual = _expected_actual_rows(execution.requested_actions, execution.actual_actions)

    outcome = OperationalOutcome(
        execution_id=execution_id,
        expected_outcome=json.dumps(expected),
        actual_outcome=json.dumps(actual),
        is_success=operational_success,
        causes=json.dumps(causes),
        operational_impact=json.dumps(operational_impact),
        planning_implications=json.dumps(planning_implications),
    )
    db.session.add(outcome)
    db.session.commit()

    return _result(
        "SUCCESS",
        {
            "execution_id": execution_id,
            "expected_outcome": expected,
            "actual_outcome": actual,
            "execution_success": execution_success,
            "command_match": command_match,
            "command_mismatches": mismatches,
            "expected_vs_actual": expected_vs_actual,
            "before_after": before_after,
            "operational_success": operational_success,
            "replan_required": goal_result in {"NOT_ACHIEVED", "PARTIAL"} or execution_status_requires_replan(execution.status),
            "goal_result": goal_result,
            "goal_criteria": goal_criteria,
            "balance_ok": balance_ok,
            "imbalance_mw": imbalance_mw,
            "reserve_ok": reserve_ok,
            "reserve_assessment_id": reserve_assessment.id if reserve_assessment is not None else None,
            "reserve_required_mw": reserve_required_mw,
            "reserve_actual_mw": reserve_actual_mw,
            "reserve_margin_mw": reserve_margin_mw,
            "reserve_status": reserve_assessment_status,
            "deviations": deviations,
            "causes": causes,
            "operational_impact": operational_impact,
            "planning_implications": planning_implications,
            "evidence_quality": evidence_quality,
            "verification_delay_minutes": verification_policy["verification_delay_minutes"],
            "verification_due_at": verification_due_at.isoformat() + "Z",
            "verification_status": "COMPLETED",
            "verification_checked_at": now.isoformat() + "Z",
            "dynamic_pricing_outcome": dynamic_pricing_outcome,
            "outcome_id": outcome.id,
        },
        "Operational outcome diagnosed from execution and post-execution evidence.",
        tool_name="assess_and_diagnose_outcome",
        input_data={"execution_id": execution_id, "post_execution_snapshot_id": post_execution_snapshot_id},
    )


def forecast_error_analysis(forecast_ids=None, actual_measurement_ids=None, lookback_days=None,
                            time_window_start=None, time_window_end=None, variable_name=None,
                            matching_profile=None):
    """Compare forecast observations with actual measurements and extract evidence-backed patterns.

    Matching tolerance and pattern thresholds come from the approved configuration.  Historical
    ForecastErrorLog records may contribute evidence to pattern detection when they fall inside
    the requested lookback window.  The tool never changes decision policy or agent behaviour.
    """
    policy = load_decision_policy()
    match_policy = policy.get("forecast_matching", {})
    tolerance_minutes = match_policy.get("tolerance_minutes")
    if tolerance_minutes is None:
        return _result(
            "REJECTED", None,
            "Forecast matching tolerance is not configured; no matching rule may be invented.",
            tool_name="forecast_error_analysis", input_data=locals()
        )
    try:
        tolerance_minutes = float(tolerance_minutes)
    except (TypeError, ValueError):
        return _result(
            "REJECTED", None,
            "Forecast matching tolerance is invalid.",
            tool_name="forecast_error_analysis", input_data=locals()
        )
    if not math.isfinite(tolerance_minutes) or tolerance_minutes < 0:
        return _result(
            "REJECTED", None,
            "Forecast matching tolerance must be finite and non-negative.",
            tool_name="forecast_error_analysis", input_data=locals()
        )

    if lookback_days is not None:
        try:
            lookback_days = float(lookback_days)
        except (TypeError, ValueError):
            return _result("REJECTED", None, "lookback_days must be a finite positive number.",
                           tool_name="forecast_error_analysis", input_data=locals())
        if not math.isfinite(lookback_days) or lookback_days <= 0:
            return _result("REJECTED", None, "lookback_days must be a finite positive number.",
                           tool_name="forecast_error_analysis", input_data=locals())

    thresholds = _load_thresholds()
    if thresholds is None:
        return _result("REJECTED", None, "No approved threshold profile is available.",
                       tool_name="forecast_error_analysis", input_data=locals())
    pattern_cfg = thresholds.get("tool15_forecast_error", {})

    # An explicit matching_profile is retained as an approved-call override mechanism used by
    # the existing project interface; it never silently invents a value when absent.
    profile = matching_profile or {}
    try:
        min_samples = int(profile.get("min_sample_size_for_pattern", pattern_cfg.get("pattern_min_sample_size")))
        ratio = float(profile.get("pattern_ratio_threshold", pattern_cfg.get("pattern_ratio_threshold")))
    except (TypeError, ValueError):
        return _result("REJECTED", None, "Tool 15 pattern thresholds are invalid.",
                       tool_name="forecast_error_analysis", input_data=locals())
    if min_samples <= 0 or not math.isfinite(ratio) or ratio <= 0 or ratio > 1:
        return _result("REJECTED", None, "Tool 15 pattern thresholds are invalid.",
                       tool_name="forecast_error_analysis", input_data=locals())

    # Error direction uses the configured decision-policy tolerance. This is a classification
    # tolerance, not a forecast-matching tolerance.
    tolerance_mw = profile.get("tolerance_mw", policy.get("hard_constraints", {}).get("residual_imbalance_tolerance_mw"))
    if tolerance_mw is None:
        return _result("REJECTED", None,
                       "Forecast error direction tolerance is not configured; no value may be invented.",
                       tool_name="forecast_error_analysis", input_data=locals())
    try:
        tolerance_mw = float(tolerance_mw)
    except (TypeError, ValueError):
        return _result("REJECTED", None, "Forecast error direction tolerance is invalid.",
                       tool_name="forecast_error_analysis", input_data=locals())
    if not math.isfinite(tolerance_mw) or tolerance_mw < 0:
        return _result("REJECTED", None, "Forecast error direction tolerance is invalid.",
                       tool_name="forecast_error_analysis", input_data=locals())

    q = Forecast.query
    if forecast_ids:
        q = q.filter(Forecast.id.in_(forecast_ids))
    if variable_name:
        q = q.filter(Forecast.variable_name == variable_name)
    if time_window_start:
        q = q.filter(Forecast.target_time >= time_window_start)
    if time_window_end:
        q = q.filter(Forecast.target_time <= time_window_end)
    forecasts = q.all()

    # lookback_days is an explicit contract input; when supplied it limits the historical
    # forecast/actual population rather than being silently ignored.
    cutoff = None
    if lookback_days is not None:
        cutoff = _now_utc() - timedelta(days=lookback_days)
        forecasts = [f for f in forecasts if f.target_time >= cutoff]

    if not forecasts:
        return _result("ERROR", None, "No forecasts found in the requested scope.",
                       tool_name="forecast_error_analysis", input_data=locals())

    actual_q = ActualMeasurement.query
    if actual_measurement_ids:
        actual_q = actual_q.filter(ActualMeasurement.id.in_(actual_measurement_ids))
    actuals = actual_q.all()
    if cutoff is not None:
        actuals = [a for a in actuals if a.timestamp >= cutoff]

    used = set()
    errors = []
    for f in forecasts:
        # A forecast with no numeric value cannot produce a ForecastErrorLog because its
        # signed_error/abs_error columns are non-nullable. Preserve the missing evidence rather
        # than converting it to zero or fabricating an error.
        if f.forecast_value is None:
            continue

        candidates = [
            a for a in actuals
            if a.id not in used
            and a.variable_name == f.variable_name
            and a.unit == f.unit
            and a.source == f.source
            and abs((a.timestamp - f.target_time).total_seconds()) <= tolerance_minutes * 60
        ]
        if not candidates:
            # The forecast provider and simulated/measurement source may legitimately differ.
            # Variable + compatible unit + time remain mandatory; source mismatch is therefore
            # allowed only as this documented fallback.
            candidates = [
                a for a in actuals
                if a.id not in used
                and a.variable_name == f.variable_name
                and a.unit == f.unit
                and abs((a.timestamp - f.target_time).total_seconds()) <= tolerance_minutes * 60
            ]
        if not candidates:
            continue

        actual = min(candidates, key=lambda a: abs((a.timestamp - f.target_time).total_seconds()))
        used.add(actual.id)
        signed = float(f.forecast_value) - float(actual.actual_value)
        abs_err = abs(signed)
        pct = None if float(actual.actual_value) == 0 else abs_err / abs(float(actual.actual_value)) * 100
        if signed > tolerance_mw:
            direction = "OVERFORECAST"
        elif signed < -tolerance_mw:
            direction = "UNDERFORECAST"
        else:
            direction = "ACCURATE"

        row = ForecastErrorLog(
            forecast_id=f.id,
            actual_id=actual.id,
            signed_error=signed,
            abs_error=abs_err,
            error_pct=pct,
            error_direction=direction,
            operational_impact=None,
            known_cause="UNKNOWN",
            pattern_label=direction,
        )
        db.session.add(row)
        db.session.flush()
        errors.append({
            "error_log_id": row.id,
            "forecast_id": f.id,
            "actual_id": actual.id,
            "signed_error": signed,
            "abs_error": abs_err,
            "error_pct": pct,
            "direction": direction,
            "known_cause": "UNKNOWN",
        })

    # Pattern evidence = newly matched observations + prior ForecastErrorLog observations in the
    # same lookback/time scope. Do not claim a cause from a directional pattern alone.
    current_error_ids = {e["error_log_id"] for e in errors}
    historical_logs = ForecastErrorLog.query.all()
    historical_directions = []
    for log in historical_logs:
        if log.id in current_error_ids:
            continue
        if log.error_direction not in {"OVERFORECAST", "UNDERFORECAST", "ACCURATE", "UNKNOWN"}:
            continue
        forecast = getattr(log, "forecast", None)
        actual = getattr(log, "actual", None)
        reference_time = getattr(forecast, "target_time", None) or getattr(actual, "timestamp", None)
        if cutoff is not None and (reference_time is None or reference_time < cutoff):
            continue
        if time_window_start and (reference_time is None or reference_time < time_window_start):
            continue
        if time_window_end and (reference_time is None or reference_time > time_window_end):
            continue
        if variable_name and (forecast is None or forecast.variable_name != variable_name):
            continue
        historical_directions.append(log.error_direction)

    directions = [e["direction"] for e in errors] + historical_directions
    patterns = []
    lessons = []
    total_observations = len(directions)
    if total_observations >= min_samples:
        for label in ("OVERFORECAST", "UNDERFORECAST"):
            count = sum(direction == label for direction in directions)
            confidence = count / total_observations if total_observations else None
            if confidence is not None and confidence >= ratio:
                pattern = "SYSTEMATIC_" + label
                evidence_summary = (
                    f"{count}/{total_observations} observations in the requested evidence window "
                    f"show {label.lower()} direction; no causal explanation is inferred."
                )
                planning_implication = "Review forecast bias; do not automatically modify policy."
                patterns.append({
                    "label": pattern,
                    "frequency": count,
                    "evidence_summary": evidence_summary,
                    "planning_implication": planning_implication,
                    "confidence": confidence,
                })

                # Link the lesson to an actual newly-created ForecastErrorLog whenever possible.
                source_error_id = errors[0]["error_log_id"] if errors else None
                if source_error_id is not None:
                    lesson = LessonMemory(
                        source_forecast_error_id=source_error_id,
                        observed_pattern=pattern,
                        evidence_summary=evidence_summary,
                        frequency_count=count,
                        confidence_score=confidence,
                        planning_implication=planning_implication,
                    )
                    db.session.add(lesson)
                    db.session.flush()
                    lessons.append(lesson.id)

    db.session.commit()
    return _result(
        "SUCCESS",
        {"errors": errors, "patterns": patterns, "lessons_created": lessons},
        "Forecast error analysis completed with configured unit/time matching and historical evidence.",
        tool_name="forecast_error_analysis",
        input_data=locals(),
    )


def retrieve_engineering_evidence(query: str, top_k: int = 3, plan_id: int | None = None, run_id: int | None = None, topic: str | None = None):
    """Tool 16: canonical RAG retrieval with persisted, traceable evidence."""
    if not query or not query.strip():
        return _result("REJECTED", None, "Retrieval query cannot be empty.",
                       tool_name="retrieve_engineering_evidence", input_data=locals())
    if run_id is None and plan_id is not None:
        plan = db.session.get(Plan, plan_id)
        if plan is not None:
            run_id = plan.run_id
    audit_input = {"query": query, "top_k": top_k, "plan_id": plan_id, "run_id": run_id, "topic": topic}
    try:
        outcome = get_engine().retrieve(query, top_k=top_k, topic=topic)
    except Exception as exc:
        return _result("FAILED", None, f"RAG retrieval failed: {exc}",
                       run_id=run_id, tool_name="retrieve_engineering_evidence", input_data=audit_input)
    results = []
    for hit in outcome.chunks:
        evidence = hit.to_evidence_dict()
        row = RAGEvidence(
            plan_id=plan_id, run_id=run_id, document_source=evidence["source"],
            source_type=evidence["source_type"], doc_metadata=evidence["document_metadata"],
            chunk_text=evidence["text"], query_used=query, retrieval_timestamp=_now_utc(),
            is_synthetic=evidence["source_type"] == "synthetic",
        )
        db.session.add(row)
        db.session.flush()
        evidence["evidence_id"] = row.id
        results.append(evidence)
    quality = outcome.evidence_quality
    status = "SUCCESS" if quality == "STRONG" else ("PARTIAL" if quality == "WEAK" else "PARTIAL")
    message = "Engineering evidence retrieved from the canonical RAG engine." if results else "No sufficient engineering evidence retrieved."
    return _result(status, {
        "query_used": query, "retrieval_timestamp": _now_utc().isoformat() + "Z",
        "results": results, "evidence_quality": quality, "best_score": outcome.best_score,
        "min_score": outcome.min_score, "embedder": outcome.embedder,
        "skipped_documents": outcome.skipped_documents,
    }, message, run_id=run_id, tool_name="retrieve_engineering_evidence", input_data=audit_input)
