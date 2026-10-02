"""SOLARGRID AI — Planning Tools 05–09 (merged implementation candidate)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy

import numpy as np
from scipy.optimize import linprog

from config_loader import load_decision_policy
from plan_lifecycle import transition_plan
from runtime import tool_call, finish_log
from schemas import (
    BatteryDispatchPoint, BatteryValidationItem, BatteryValidationResult,
    CandidatePlan, ConstraintViolation, DispatchInterval, DomainStatus,
    EvaluatePlansResult, GeneratePlansRequest, GeneratePlansResult, DynamicPricingResult,
    GeneratorDispatchPoint, GeneratorValidationItem, GeneratorValidationResult,
    NetworkValidationStatus, PlanDispatch, PlanEvaluationRequest,
    PlanEvaluationResult, PlanValidationRequest, PowerFlowRequest,
    PowerFlowResult, PowerFlowStatus, Strategy, ToolStatus,
)
from tool_common import plan_dispatch_from_record, resolve_dynamic_pricing_context_ref, utc


# ---------------- Tool 05 ----------------

TOOL_05_NAME="generate_and_optimize_plans"
TOOL_VERSION="1.0"
HIGH_SLACK_PENALTY=1_000_000.0


def _var_key(kind,asset_id,t): return f"{kind}:{asset_id}:{t}"

def _policy_status(policy):
    return str(policy.get("status","")).upper() in {"APPROVED","ACTIVE","CONFIGURED"}

def _interval_minutes(policy):
    value=policy.get("interval_minutes")
    if value is None:
        raise ValueError("SPECIFICATION ISSUE: interval_minutes is OPEN/TEAM DECISION and no approved value is configured")
    value=int(value)
    if value<=0: raise ValueError("Invalid configured interval_minutes")
    return value

def _snapshot_assets(snapshot, generators, batteries):
    sg={str(x.get("id")):x for x in (snapshot.state_json or {}).get("generators",[]) if x.get("id") is not None}
    sb={str(x.get("id")):x for x in (snapshot.state_json or {}).get("batteries",[]) if x.get("id") is not None}
    if any(str(g.id) not in sg for g in generators) or any(str(b.id) not in sb for b in batteries):
        raise ValueError("UNKNOWN historical asset state in SystemSnapshot.state_json")
    return sg,sb

def _build_candidate(snapshot,generators,batteries,series,strategy,policy,horizon_minutes,objectives,reserve_required=None):
    interval_minutes=_interval_minutes(policy)
    if horizon_minutes<=0 or horizon_minutes%interval_minutes:
        raise ValueError("horizon_minutes must be positive and divisible by approved interval_minutes")
    if strategy==Strategy.MIXED and not policy.get("mixed_objective"):
        raise ValueError("SPECIFICATION ISSUE: approved MIXED objective is not configured")
    if strategy==Strategy.MIXED and policy.get("mixed_objective",{}).get("type") != "min_total_dispatch_movement":
        raise ValueError("SPECIFICATION ISSUE: approved MIXED objective type is not supported by the existing LP boundary")
    if strategy==Strategy.MIXED and policy.get("mixed_objective",{}).get("movement_weight") is None:
        raise ValueError("SPECIFICATION ISSUE: MIXED movement_weight is not approved/configured")
    sg,sb=_snapshot_assets(snapshot,generators,batteries)
    n=len(series); dt=interval_minutes/60.0
    names=[]; bounds=[]; c=[]; Aeq=[]; beq=[]; Aub=[]; bub=[]
    costs=(policy.get("cost_model",{}).get("generation_cost_usd_per_mwh") or policy.get("cost_model",{}).get("fuel_cost_usd_per_mwh",{}))
    if strategy==Strategy.COST_MIN and not costs:
        raise ValueError("SPECIFICATION ISSUE: approved generator cost model is missing")
    # SolarGrid is a solar-only fleet. Generator rows are the actual controllable
    # solar plants, not extra generation on top of snapshot.solar_gen_mw.
    #
    # Strategy semantics:
    # - SOLAR_ONLY: solar generators may move; battery is held at 0 MW.
    # - BATTERY_ONLY: solar generators follow the forecast-scaled baseline and
    #   only the battery may actively correct the balance.
    # - MIXED/COST_MIN: both solar generators and the battery may move.
    gen_controlled = strategy != Strategy.BATTERY_ONLY
    bat_allowed = strategy != Strategy.SOLAR_ONLY

    snapshot_solar = float(snapshot.solar_gen_mw) if snapshot.solar_gen_mw is not None else None

    def baseline_generator_output(state, point):
        """Forecast-scaled no-redispatch output used by BATTERY_ONLY.

        Forecast solar is an aggregate fleet forecast.  It must never be added on
        top of generator dispatch.  For a battery-only candidate we preserve the
        current plant split and scale it by the aggregate forecast change.
        """
        current = float(state.get("current_output_mw") or 0.0)
        forecast_solar = float(point.get("solar") or 0.0)
        if snapshot_solar is None or snapshot_solar <= 1e-9:
            target = current
        else:
            target = current * forecast_solar / snapshot_solar
        if forecast_solar <= 1e-9:
            return 0.0
        lo = float(state.get("min_output_mw") or 0.0)
        hi = float(state.get("max_output_mw") or 0.0)
        return max(lo, min(hi, target))

    # named variables: generator output, battery charge/discharge, deficit,
    # surplus, reserve slack. Solar curtailment is derived from the solved
    # generator output rather than modeled as a second generation term.
    def add(name,bound,obj): names.append(name); bounds.append(bound); c.append(obj); return len(names)-1
    for t,p in enumerate(series):
        for g in generators:
            s=sg[str(g.id)]
            if str(s.get("availability_status")).upper()!="AVAILABLE":
                lo=hi=0.0
            else:
                vals=(s.get("min_output_mw"),s.get("max_output_mw"),s.get("ramp_rate_mw_per_min"),s.get("current_output_mw"))
                if any(v is None for v in vals): raise ValueError(f"UNKNOWN generator physical data for {g.id}")
                if gen_controlled:
                    lo=float(vals[0]); hi=float(vals[1])
                else:
                    baseline=baseline_generator_output(s,p)
                    lo=hi=baseline
            #new
            policy_key = str(g.name) if g.name is not None else None

            if strategy == Strategy.COST_MIN:
                if not policy_key or policy_key not in costs:
                     raise ValueError(
                        f"SPECIFICATION ISSUE: approved generation cost is missing "
                        f"for generator policy key {policy_key!r} (DB id={g.id})"
                            )
                fuel = float(costs[policy_key])
            else:
                fuel = 0.0
            #new
            #if strategy==Strategy.COST_MIN and str(g.id) not in costs:
            #    raise ValueError(f"SPECIFICATION ISSUE: approved fuel cost is missing for generator {g.id}")
            #fuel=float(costs[str(g.id)]) if str(g.id) in costs else 0.0
            obj=fuel*dt if strategy==Strategy.COST_MIN else 0.0
            # MIXED objective is policy-owned. The approved objective must explicitly be movement-based.
            if strategy==Strategy.MIXED and policy.get("mixed_objective",{}).get("type") == "min_total_dispatch_movement":
                obj=0.0
            add(_var_key("g",str(g.id),t),(lo,hi),obj)
            if strategy==Strategy.MIXED:
                add(_var_key("gm",str(g.id),t),(0.0,None),float(policy["mixed_objective"]["movement_weight"]))
        for b in batteries:
            s=sb[str(b.id)]
            if not bat_allowed or str(s.get("availability_status")).upper()!="AVAILABLE":
                add(_var_key("bc",str(b.id),t),(0.0,0.0),0.0); add(_var_key("bd",str(b.id),t),(0.0,0.0),0.0)
            else:
                vals=(s.get("max_charge_mw"),s.get("max_discharge_mw"),s.get("capacity_mwh"),s.get("soc_pct"),s.get("min_soc_pct"),s.get("max_soc_pct"),s.get("efficiency"))
                if any(v is None for v in vals): raise ValueError(f"UNKNOWN battery physical data for {b.id}")
                if float(s["capacity_mwh"])<=0 or not (0<float(s["efficiency"])<=1): raise ValueError(f"Invalid battery physical data for {b.id}")
                add(_var_key("bc",str(b.id),t),(0.0,float(s["max_charge_mw"])),0.0)
                add(_var_key("bd",str(b.id),t),(0.0,float(s["max_discharge_mw"])),0.0)
        add(_var_key("deficit","system",t),(0.0,None),HIGH_SLACK_PENALTY*dt)
        add(_var_key("surplus","system",t),(0.0,None),HIGH_SLACK_PENALTY*dt)
    reserve_idx=add(_var_key("reserve_shortfall","system",0),(0.0,None),HIGH_SLACK_PENALTY)
    idx={n:i for i,n in enumerate(names)}
    for t,p in enumerate(series):
        row=np.zeros(len(names))
        # Generator output is the solar generation. Do not add the aggregate
        # solar forecast again; doing so double-counts the same fleet.
        for g in generators: row[idx[_var_key("g",str(g.id),t)]]=1.0
        for b in batteries:
            row[idx[_var_key("bd",str(b.id),t)]]=1.0; row[idx[_var_key("bc",str(b.id),t)]]=-1.0
        row[idx[_var_key("deficit","system",t)]]=1.0; row[idx[_var_key("surplus","system",t)]]=-1.0
        Aeq.append(row); beq.append(float(p["demand"]))
    if strategy==Strategy.MIXED:
        for g in generators:
            s=sg[str(g.id)]; prev0=s.get("current_output_mw")
            if prev0 is None: raise ValueError(f"UNKNOWN historical current output for {g.id}")
            for t in range(n):
                row=np.zeros(len(names)); row[idx[_var_key("gm",str(g.id),t)]]=-1; row[idx[_var_key("g",str(g.id),t)]]=1
                if t==0:
                    Aub.append(row.copy()); bub.append(float(prev0))
                    row=np.zeros(len(names)); row[idx[_var_key("gm",str(g.id),t)]]=-1; row[idx[_var_key("g",str(g.id),t)]]=-1
                    Aub.append(row.copy()); bub.append(-float(prev0))
                else:
                    row2=row.copy(); row2[idx[_var_key("g",str(g.id),t-1)]]=-1; Aub.append(row2); bub.append(0.0)
                    row3=np.zeros(len(names)); row3[idx[_var_key("gm",str(g.id),t)]]=-1; row3[idx[_var_key("g",str(g.id),t)]]=-1; row3[idx[_var_key("g",str(g.id),t-1)]]=1
                    Aub.append(row3); bub.append(0.0)
    for g in generators:
        s=sg[str(g.id)]; ramp=s.get("ramp_rate_mw_per_min")
        if ramp is None: raise ValueError(f"UNKNOWN generator ramp data for {g.id}")
        if str(s.get("availability_status")).upper()!="AVAILABLE": continue
        lim=float(ramp)*interval_minutes
        for t in range(n):
            row=np.zeros(len(names)); row[idx[_var_key("g",str(g.id),t)]]=1
            if t==0:
                prev=s.get("current_output_mw");
                if prev is None: raise ValueError(f"UNKNOWN historical current output for {g.id}")
                Aub.extend([row.copy(),-row.copy()]); bub.extend([float(prev)+lim,-float(prev)+lim])
            else:
                row[idx[_var_key("g",str(g.id),t-1)]]=-1; Aub.extend([row.copy(),-row.copy()]); bub.extend([lim,lim])
    # Exact linear battery energy trajectory via separate charge/discharge variables.
    for b in batteries:
        s=sb[str(b.id)]
        if not bat_allowed or str(s.get("availability_status")).upper()!="AVAILABLE": continue
        cap=float(s["capacity_mwh"]); eff=float(s["efficiency"]); emin=cap*float(s["min_soc_pct"])/100; emax=cap*float(s["max_soc_pct"])/100; e0=cap*float(s["soc_pct"])/100
        for t in range(n):
            row=np.zeros(len(names))
            for k in range(t+1):
                row[idx[_var_key("bd",str(b.id),k)]] += dt/eff
                row[idx[_var_key("bc",str(b.id),k)]] += -dt*eff
            # E = e0 - row; enforce emin <= e0-row <= emax
            Aub.append(row.copy()); bub.append(e0-emin)
            Aub.append(-row.copy()); bub.append(emax-e0)
    if reserve_required is not None:
        # First-interval headroom, with battery energy-limited deliverability at interval 0.
        row=np.zeros(len(names)); constant=0.0
        for g in generators:
            s=sg[str(g.id)]
            if gen_controlled and str(s.get("availability_status")).upper()=="AVAILABLE":
                row[idx[_var_key("g",str(g.id),0)]]=1.0; constant+=float(s["max_output_mw"])
        for b in batteries:
            s=sb[str(b.id)]
            if bat_allowed and str(s.get("availability_status")).upper()=="AVAILABLE":
                # Remaining first-interval battery reserve is limited by both power and
                # deliverable energy. Scheduled discharge consumes part of that headroom.
                energy_mwh = max(
                    0.0,
                    (float(s["soc_pct"]) - float(s["min_soc_pct"])) / 100.0
                    * float(s["capacity_mwh"]) * float(s["efficiency"])
                )
                energy_limited_mw = energy_mwh / dt
                available_discharge = min(float(s["max_discharge_mw"]), energy_limited_mw)
                row[idx[_var_key("bd",str(b.id),0)]] += 1.0
                constant += available_discharge
        row[reserve_idx]=-1.0; Aub.append(row); bub.append(constant-float(reserve_required))
    res=linprog(np.asarray(c),A_ub=np.asarray(Aub) if Aub else None,b_ub=np.asarray(bub) if bub else None,A_eq=np.asarray(Aeq),b_eq=np.asarray(beq),bounds=bounds,method="highs")
    status={0:"OPTIMAL",1:"FAILED",2:"INFEASIBLE",3:"UNBOUNDED",4:"UNKNOWN"}.get(res.status,"UNKNOWN")
    if res.x is None:
        return {"solver_status":status,"optimization_status":status,"solver_message":res.message,"x":None,"slack_diagnostics":[]},None
    intervals=[]; start=utc(snapshot.timestamp)
    slack=[]
    for t in range(n):
        gens={str(g.id):GeneratorDispatchPoint(output_mw=float(res.x[idx[_var_key("g",str(g.id),t)]])) for g in generators}
        bats={str(b.id):BatteryDispatchPoint(power_mw=float(res.x[idx[_var_key("bd",str(b.id),t)]]-res.x[idx[_var_key("bc",str(b.id),t)]])) for b in batteries}
        deficit=float(res.x[idx[_var_key("deficit","system",t)]]); surplus=float(res.x[idx[_var_key("surplus","system",t)]])
        dispatched_solar=sum(point.output_mw for point in gens.values())
        # Curtailment is descriptive: forecast solar that the plan elects not to
        # dispatch. It is not subtracted again from dispatched generation.
        curtail=max(0.0, float(series[t]["solar"]) - float(dispatched_solar))
        if deficit>1e-9: slack.append({"interval_index":t,"constraint":"unserved_energy","slack_mw":deficit})
        if surplus>1e-9: slack.append({"interval_index":t,"constraint":"surplus_energy","slack_mw":surplus})
        intervals.append(DispatchInterval(index=t,start=start+timedelta(minutes=t*interval_minutes),generators=gens,batteries=bats,solar_curtailment_mw=curtail,residual_imbalance_mw=deficit-surplus))
    reserve_short=float(res.x[reserve_idx]);
    if reserve_short>1e-9: slack.append({"interval_index":0,"constraint":"reserve_shortfall","slack_mw":reserve_short})
    return {"solver_status":status,"optimization_status":"INFEASIBLE" if slack else "FEASIBLE","solver_message":res.message,"dispatch":PlanDispatch(interval_minutes=interval_minutes,start_time=start,intervals=intervals),"objective":float(res.fun),"reserve_shortfall":reserve_short,"slack_diagnostics":slack},None

def _resolve_dynamic_pricing_context_ref(ref, *, expected_snapshot_id, expected_horizon_minutes, db, ToolCallLog, snapshot, interval_minutes, consumer_name):
    """Compatibility wrapper around the shared Tool 17 provenance boundary."""
    return resolve_dynamic_pricing_context_ref(
        ref,
        expected_snapshot_id=expected_snapshot_id,
        expected_horizon_minutes=expected_horizon_minutes,
        db=db,
        ToolCallLog=ToolCallLog,
        snapshot=snapshot,
        interval_minutes=interval_minutes,
        consumer_name=consumer_name,
    )


def _resolve_dynamic_pricing_planning_context(request, *, db, ToolCallLog, snapshot, interval_minutes):
    return _resolve_dynamic_pricing_context_ref(
        request.dynamic_pricing_context,
        expected_snapshot_id=request.snapshot_id,
        expected_horizon_minutes=request.horizon_minutes,
        db=db,
        ToolCallLog=ToolCallLog,
        snapshot=snapshot,
        interval_minutes=interval_minutes,
        consumer_name="Tool 05",
    )


def _dynamic_pricing_effective_demand_profile(*, dispatch, snapshot, Forecast, dynamic_pricing_result):
    """Rebuild the exact demand profile used by Tool 05 for a persisted DP plan.

    This is used by Tools 08/09. Forecast demand is read at the END boundary of
    each dispatch interval (T+15, T+30, ...), then the trusted Tool 17 deltas are
    applied with the same helper used by Tool 05.
    """
    horizon_minutes = dispatch.interval_minutes * len(dispatch.intervals)
    expected_times = [
        utc(snapshot.timestamp) + timedelta(minutes=(i + 1) * dispatch.interval_minutes)
        for i in range(len(dispatch.intervals))
    ]
    rows = Forecast.query.filter(
        Forecast.snapshot_id == snapshot.id,
        Forecast.target_time >= expected_times[0],
        Forecast.target_time <= expected_times[-1],
    ).all()
    by_time = {}
    for row in rows:
        if str(getattr(row, "unit", "")).upper() == "MW" and getattr(row, "variable_name", None) == "demand" and getattr(row, "forecast_value", None) is not None:
            by_time[utc(row.target_time)] = float(row.forecast_value)
    base = []
    for index, target_time in enumerate(expected_times):
        if target_time not in by_time:
            raise ValueError(
                f"Missing approved demand MW forecast at T+{(index + 1) * dispatch.interval_minutes} "
                f"({target_time.isoformat()}) for snapshot_id={snapshot.id}"
            )
        base.append({"target_time": target_time, "demand": by_time[target_time]})
    effective = _apply_dynamic_pricing_demand_adjustments(base, dynamic_pricing_result)
    return [float(point["demand"]) for point in effective]


def _apply_dynamic_pricing_demand_adjustments(series, dynamic_pricing_result):
    """Return Tool 05's effective-demand series without mutating the base forecasts.

    Legacy planning passes ``None`` and receives a value-equal copy of the
    original series.  When Tool 17 context exists, only demand is adjusted;
    solar forecasts and all other planning semantics remain unchanged.
    """
    effective = [dict(point) for point in series]
    if dynamic_pricing_result is None:
        return effective

    adjustments = {
        item.interval_index: float(item.demand_delta_mw)
        for item in dynamic_pricing_result.demand_adjustments
    }
    for index, point in enumerate(effective):
        base_demand = float(point["demand"])
        delta = adjustments.get(index, 0.0)
        adjusted = base_demand + delta
        if adjusted < -1e-9:
            raise ValueError(
                f"Dynamic Pricing would make demand negative at interval {index}: "
                f"base={base_demand}, delta={delta}"
            )
        point["demand"] = max(0.0, adjusted)
        point["base_demand_mw"] = base_demand
        point["dynamic_pricing_demand_delta_mw"] = delta
    return effective



def _dynamic_pricing_candidate_metadata(dynamic_pricing_result, tool_call_id, series):
    if dynamic_pricing_result is None:
        return None
    return {
        "source_tool": "analyze_dynamic_pricing",
        "tool_call_id": int(tool_call_id),
        "snapshot_id": int(dynamic_pricing_result.snapshot_id),
        "horizon_minutes": int(dynamic_pricing_result.horizon_minutes),
        "config_version": dynamic_pricing_result.config_version,
        "energy_scope": dynamic_pricing_result.energy_scope,
        "total_shifted_mw": dynamic_pricing_result.total_shifted_mw,
        "demand_adjustments": [
            item.model_dump(mode="json") for item in dynamic_pricing_result.demand_adjustments
        ],
        "effective_demand_mw": [float(point["demand"]) for point in series],
        "base_demand_mw": [float(point.get("base_demand_mw", point["demand"])) for point in series],
        "validation_integration": "DP_AWARE_TOOLS_08_09",
        "execution_integration": "DP_AWARE_SIMULATION_TOOL13",
    }


def _persist_candidate(db,Plan,snapshot,request,candidate,run_id):
    d=candidate.dispatch.model_dump(mode="json")
    gen={str(g):{"output_mw":[iv.generators[str(g)].output_mw for iv in candidate.dispatch.intervals]} for g in {g for iv in candidate.dispatch.intervals for g in iv.generators}}
    bat={str(b):{"power_mw":[iv.batteries[str(b)].power_mw for iv in candidate.dispatch.intervals]} for b in {b for iv in candidate.dispatch.intervals for b in iv.batteries}}
    plan=Plan(run_id=run_id,snapshot_id=snapshot.id,imbalance_event_id=request.risk_event_id,plan_name=candidate.plan_name,
              horizon_hours=int(round(request.horizon_minutes/60.0)),candidate_id=candidate.candidate_id,strategy_label=candidate.strategy.value,
              optimization_status=candidate.optimization_status,actions=d,gen_dispatch_json=gen,battery_dispatch_json=bat,
              expected_reserve_mw=candidate.expected_reserve_mw,assumptions="; ".join(candidate.assumptions),status="PROPOSED")
    db.session.add(plan); db.session.flush(); candidate.plan_id=plan.id; return plan

def generate_and_optimize_plans(payload,*,db,models,run_id=None):
    request=payload if isinstance(payload,GeneratePlansRequest) else GeneratePlansRequest.model_validate(payload)
    Plan=models["Plan"]; Snapshot=models["SystemSnapshot"]; Generator=models["Generator"]; Battery=models["Battery"]; Forecast=models["Forecast"]; ReserveAssessment=models["ReserveAssessment"]
    AgentRunTrace=models["AgentRunTrace"]; ToolCallLog=models["ToolCallLog"]
    with tool_call(db,AgentRunTrace,ToolCallLog,tool_name=TOOL_05_NAME,tool_category="PLANNING",input_payload=request.model_dump(mode="json"),run_id=run_id) as (rid,log):
        try:
            snapshot=db.session.get(Snapshot, request.snapshot_id)
            if snapshot is None: raise ValueError(f"Unknown snapshot_id: {request.snapshot_id}")
            policy=load_decision_policy(request.decision_policy_version)
            if not _policy_status(policy):
                out={"tool_status":"REJECTED","domain_status":"UNKNOWN","data":None,"message":"SPECIFICATION ISSUE: Decision Policy is marked ASSUMPTION_PENDING_TEAM_APPROVAL; Tool 05 cannot safely optimize using unapproved reserve/cost/objective policy."}
                finish_log(log,out,ToolStatus.REJECTED); db.session.commit(); return out
            interval=_interval_minutes(policy)
            if request.horizon_minutes%interval: raise ValueError("horizon_minutes must be divisible by approved interval_minutes")
            requested=request.strategies or [Strategy.SOLAR_ONLY,Strategy.BATTERY_ONLY,Strategy.MIXED,Strategy.COST_MIN]
            strategies=[]
            for s in requested:
                if s not in strategies: strategies.append(s)
            if request.required_candidate_count > len(strategies):
                message = (
                    f"SPECIFICATION ISSUE: requested {request.required_candidate_count} genuinely different candidates, "
                    f"but only {len(strategies)} configured strategies are available"
                )
                out={"tool_status":"REJECTED","domain_status":"UNKNOWN","data":None,"message":message}
                finish_log(log,out,ToolStatus.REJECTED); db.session.commit(); return out
            strategies=strategies[:request.required_candidate_count]
            # Only MW forecasts are eligible. No irradiance-to-MW conversion occurs here.
            rows=Forecast.query.filter(Forecast.snapshot_id==request.snapshot_id,Forecast.target_time>=utc(snapshot.timestamp),Forecast.target_time<=utc(snapshot.timestamp)+timedelta(minutes=request.horizon_minutes)).all()
            by={}
            for f in rows:
                if str(f.unit).upper()=="MW" and f.variable_name in {"solar","demand"} and f.forecast_value is not None:
                    by.setdefault(utc(f.target_time),{})[f.variable_name]=float(f.forecast_value)
            count=request.horizon_minutes//interval
            expected_times=[utc(snapshot.timestamp)+timedelta(minutes=i*interval) for i in range(count+1)]
            complete_points=[]
            for i,t in enumerate(expected_times):
                point=by.get(t,{})
                missing=[name for name in ("solar","demand") if point.get(name) is None]
                if missing:
                    offset=i*interval
                    raise ValueError(
                        f"Missing approved {'/'.join(missing)} MW forecast at T+{offset} "
                        f"({t.isoformat()}) for snapshot_id={request.snapshot_id}"
                    )
                complete_points.append({"target_time":t,"solar":point["solar"],"demand":point["demand"]})
            # A 120-minute horizon contains eight 15-minute dispatch intervals.
            # Each command is applied over [T, T+15] and the simulator verifies the
            # state after advancing to the interval end, so dispatch must target the
            # END boundary forecasts T+15 ... T+120.  Using T+0 ... T+105 caused
            # the final post-execution snapshot at T+120 to be compared against a
            # demand/solar boundary the optimizer had never dispatched for.
            base_series=complete_points[1:]
            resolved_dynamic_pricing_result = _resolve_dynamic_pricing_planning_context(
                request, db=db, ToolCallLog=ToolCallLog, snapshot=snapshot, interval_minutes=interval
            )
            # A successful Tool 17 result that recommends no action is a strict
            # no-op for Tool 05.  Only an actual conserved adjustment set activates
            # the Dynamic Pricing planning branch.
            dynamic_pricing_result = (
                resolved_dynamic_pricing_result
                if resolved_dynamic_pricing_result is not None
                and resolved_dynamic_pricing_result.demand_adjustments
                else None
            )
            series = _apply_dynamic_pricing_demand_adjustments(base_series, dynamic_pricing_result)
            dynamic_pricing_metadata = _dynamic_pricing_candidate_metadata(
                dynamic_pricing_result,
                request.dynamic_pricing_context.tool_call_id if dynamic_pricing_result is not None else None,
                series,
            )
            generators=Generator.query.order_by(Generator.id).all(); batteries=Battery.query.order_by(Battery.id).all()
            if not generators and not batteries: raise ValueError("No dispatchable resources are configured")
            reserve=None
            if request.reserve_assessment_id is not None: reserve=db.session.get(ReserveAssessment, request.reserve_assessment_id)
            else:
                matches=ReserveAssessment.query.filter_by(snapshot_id=request.snapshot_id).all()
                exact=[r for r in matches if r.time_horizon_minutes is not None and abs(float(r.time_horizon_minutes)-request.horizon_minutes)<1e-9]
                if len(exact)==1: reserve=exact[0]
                elif len(exact)>1: raise ValueError("Ambiguous context-matched ReserveAssessment")
            reserve_required=reserve.required_reserve_mw if reserve else None
            candidates=[]
            for strategy in strategies:
                built,_=_build_candidate(snapshot,generators,batteries,series,strategy,policy,request.horizon_minutes,request.objectives,reserve_required)
                if built.get("dispatch") is None:
                    # Preserve an infeasible/unknown candidate with explicit status and no fabricated dispatch values.
                    placeholders=[DispatchInterval(index=i,start=utc(snapshot.timestamp)+timedelta(minutes=i*interval),generators={},batteries={},solar_curtailment_mw=None,residual_imbalance_mw=None) for i in range(count)]
                    metrics={"slack_diagnostics":built.get("slack_diagnostics",[])}
                    assumptions=["Candidate preserved despite solver infeasibility/unknown status.","No final selection is performed by Tool 05."]
                    if dynamic_pricing_metadata is not None:
                        metrics["dynamic_pricing_context"] = dynamic_pricing_metadata
                        assumptions.append(
                            f"Dynamic Pricing Tool 17 context from ToolCallLog {request.dynamic_pricing_context.tool_call_id} adjusted forecast demand before optimization."
                        )
                        assumptions.append(
                            "Dynamic Pricing provenance is persisted in PlanDispatch for DP-aware validation and DP-aware simulated execution."
                        )
                    candidate=CandidatePlan(candidate_id=f"{strategy.value}-{snapshot.id}",plan_name=f"{strategy.value} candidate",strategy=strategy,dispatch=PlanDispatch(interval_minutes=interval,start_time=utc(snapshot.timestamp),intervals=placeholders),optimization_status=built.get("optimization_status","UNKNOWN"),solver_status=built.get("solver_status"),expected_balance_mw=None,expected_reserve_mw=None,objective_metrics=metrics,assumptions=assumptions,domain_status=DomainStatus.INFEASIBLE if built.get("optimization_status")=="INFEASIBLE" else DomainStatus.UNKNOWN)
                else:
                    d=built["dispatch"]; max_res=max(abs(float(iv.residual_imbalance_mw)) for iv in d.intervals if iv.residual_imbalance_mw is not None)
                    reserve_metric=None if reserve is None else reserve.actual_reserve_mw
                    domain=DomainStatus.INFEASIBLE if built.get("optimization_status")=="INFEASIBLE" else DomainStatus.NOT_EVALUATED
                    metrics={"objective_value":built.get("objective"),"slack_diagnostics":built.get("slack_diagnostics",[])}
                    assumptions=["Solar generator output is the authoritative dispatched solar generation; aggregate solar forecasts are not added a second time.","Tool 06/07 independently validate dispatch.","Tool 09 owns final selection."]
                    if dynamic_pricing_metadata is not None:
                        metrics["dynamic_pricing_context"] = dynamic_pricing_metadata
                        assumptions.append(
                            f"Dynamic Pricing Tool 17 context from ToolCallLog {request.dynamic_pricing_context.tool_call_id} adjusted forecast demand before optimization."
                        )
                        assumptions.append(
                            "Dynamic Pricing provenance is persisted in PlanDispatch for DP-aware validation and DP-aware simulated execution."
                        )
                    candidate=CandidatePlan(candidate_id=f"{strategy.value}-{snapshot.id}",plan_name=f"{strategy.value} candidate",strategy=strategy,dispatch=d,
                        generator_dispatch={str(g.id):{"output_mw":[iv.generators[str(g.id)].output_mw for iv in d.intervals]} for g in generators},
                        battery_dispatch={str(b.id):{"power_mw":[iv.batteries[str(b.id)].power_mw for iv in d.intervals]} for b in batteries},expected_balance_mw=max_res,expected_reserve_mw=reserve_metric,
                        objective_metrics=metrics,assumptions=assumptions,optimization_status=built.get("optimization_status","UNKNOWN"),solver_status=built.get("solver_status"),hard_constraint_slack_mw=(built.get("reserve_shortfall") if built.get("reserve_shortfall") else None),domain_status=domain)
                if dynamic_pricing_result is not None:
                    candidate.dispatch = candidate.dispatch.model_copy(
                        update={"dynamic_pricing_context": request.dynamic_pricing_context}
                    )
                _persist_candidate(db,Plan,snapshot,request,candidate,rid)
                candidates.append(candidate)
            result=GeneratePlansResult(snapshot_id=snapshot.id,plans=candidates,domain_status=DomainStatus.OK if candidates else DomainStatus.UNKNOWN)
            out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.SUCCESS); db.session.commit(); return out
        except Exception:
            db.session.rollback(); raise

# ---------------- Tool 06 ----------------


TOOL_06_NAME="check_generator_constraints"

def check_generator_constraints(payload, *, db, models, run_id=None):
    request=payload if isinstance(payload,PlanValidationRequest) else PlanValidationRequest.model_validate(payload)
    Plan=models["Plan"]; Generator=models["Generator"]; Snapshot=models["SystemSnapshot"]
    AgentRunTrace=models["AgentRunTrace"]; ToolCallLog=models["ToolCallLog"]
    with tool_call(db,AgentRunTrace,ToolCallLog,tool_name=TOOL_06_NAME,tool_category="VALIDATION",input_payload=request.model_dump(mode="json"),run_id=run_id) as (rid,log):
        try:
            plan=db.session.get(Plan, request.plan_id)
            if plan is None: raise ValueError(f"Unknown plan_id: {request.plan_id}")
            snapshot_id = request.context_snapshot_id or plan.snapshot_id
            snapshot=db.session.get(Snapshot, snapshot_id)
            if snapshot is None: raise ValueError("Validation snapshot is missing")
            historical={str(x.get("id")):x for x in (snapshot.state_json or {}).get("generators",[]) if x.get("id") is not None}
            dispatch=plan_dispatch_from_record(plan,PlanDispatch)
            items=[]; any_unknown=False; any_violation=False
            generators = Generator.query.order_by(Generator.id).all()
            known_generator_ids = {str(g.id) for g in generators}
            referenced_generator_ids = {str(gid) for iv in dispatch.intervals for gid in iv.generators.keys()}
            unknown_generator_ids = sorted(referenced_generator_ids - known_generator_ids)
            for gid in unknown_generator_ids:
                items.append(GeneratorValidationItem(
                    generator_id=gid,
                    interval_results=[{
                        "interval_index": iv.index,
                        "requested_output_mw": (iv.generators.get(gid).output_mw if iv.generators.get(gid) else None),
                        "feasible": False,
                    } for iv in dispatch.intervals if gid in iv.generators],
                    requested_output_mw=None,
                    feasible=False,
                    violations=[ConstraintViolation(
                        resource="generator", resource_id=gid, constraint="UNKNOWN_RESOURCE",
                        message=f"Plan references generator {gid}, but it does not exist in the authoritative generator registry",
                    )],
                    maximum_feasible_change_mw=None,
                ))
                any_violation=True
            for g in generators:
                hs=historical.get(str(g.id))
                violations=[]; interval_results=[]; unknown=False
                if hs is None:
                    unknown=True
                    interval_results=[{"interval_index":iv.index,"requested_output_mw":(iv.generators.get(str(g.id)).output_mw if iv.generators.get(str(g.id)) else None),"feasible":None} for iv in dispatch.intervals]
                    items.append(GeneratorValidationItem(generator_id=str(g.id),interval_results=interval_results,requested_output_mw=None,feasible=None,violations=[ConstraintViolation(resource="generator",resource_id=str(g.id),constraint="HISTORICAL_STATE",message="Historical generator state is missing")],maximum_feasible_change_mw=None))
                    any_unknown=True; continue
                prev=hs.get("current_output_mw")
                min_out=hs.get("min_output_mw"); max_out=hs.get("max_output_mw"); ramp=hs.get("ramp_rate_mw_per_min"); avail=hs.get("availability_status"); configured=hs.get("constraints_json") or {}
                for iv in dispatch.intervals:
                    point=iv.generators.get(str(g.id)); requested=None if point is None else point.output_mw
                    if requested is None or prev is None or min_out is None or max_out is None or ramp is None or avail is None:
                        unknown=True; interval_results.append({"interval_index":iv.index,"requested_output_mw":requested,"feasible":None}); prev=requested; continue
                    ok=True
                    unavailable = str(avail).upper() != "AVAILABLE"
                    if unavailable:
                        # Offline / tripped units are allowed to remain at 0 MW.
                        # The previous validator incorrectly applied the online
                        # minimum-output limit to an unavailable unit, so every
                        # otherwise-valid plan that kept a tripped solar plant at
                        # zero was rejected as INFEASIBLE.
                        if abs(requested) > 1e-9:
                            ok=False
                            violations.append(ConstraintViolation(
                                resource="generator", resource_id=str(g.id), interval_index=iv.index,
                                constraint="availability", observed=avail, requested=requested,
                                limit=0.0, message=f"Generator {g.id} is unavailable and must remain at 0 MW",
                            ))
                        interval_results.append({"interval_index":iv.index,"requested_output_mw":requested,"feasible":ok})
                        prev=requested
                        continue

                    # Minimum/maximum/ramp limits are online operating limits and
                    # apply only while the unit is AVAILABLE.
                    if requested<min_out:
                        ok=False; violations.append(ConstraintViolation(resource="generator",resource_id=str(g.id),interval_index=iv.index,constraint="min_output_mw",observed=requested,requested=requested,limit=min_out,excess=min_out-requested,message=f"Requested output below generator {g.id} minimum"))
                    if requested>max_out:
                        ok=False; violations.append(ConstraintViolation(resource="generator",resource_id=str(g.id),interval_index=iv.index,constraint="max_output_mw",observed=requested,requested=requested,limit=max_out,excess=requested-max_out,message=f"Requested output exceeds generator {g.id} maximum"))
                    delta=abs(requested-prev); limit=ramp*dispatch.interval_minutes
                    if delta>limit+1e-9:
                        ok=False; violations.append(ConstraintViolation(resource="generator",resource_id=str(g.id),interval_index=iv.index,constraint="ramp_rate_mw_per_min",observed=delta,requested=requested,limit=limit,excess=delta-limit,message=f"Generator {g.id} ramp limit exceeded"))
                    if isinstance(configured,dict) and configured.get("max_change_mw") is not None:
                        c=float(configured["max_change_mw"])
                        if delta>c+1e-9:
                            ok=False; violations.append(ConstraintViolation(resource="generator",resource_id=str(g.id),interval_index=iv.index,constraint="configured_max_change_mw",observed=delta,requested=requested,limit=c,excess=delta-c,message=f"Configured generator {g.id} change limit exceeded"))
                    interval_results.append({"interval_index":iv.index,"requested_output_mw":requested,"feasible":ok}); prev=requested
                feasible=None if unknown and not violations else False if violations else True
                any_unknown |= unknown; any_violation |= bool(violations)
                items.append(GeneratorValidationItem(generator_id=str(g.id),interval_results=interval_results,requested_output_mw=interval_results[0]["requested_output_mw"] if interval_results else None,feasible=feasible,violations=violations,maximum_feasible_change_mw=(ramp*dispatch.interval_minutes if ramp is not None else None)))
            aggregate=False if any_violation else None if any_unknown else True
            domain=DomainStatus.INFEASIBLE if any_violation else DomainStatus.UNKNOWN if any_unknown else DomainStatus.FEASIBLE
            result=GeneratorValidationResult(plan_id=plan.id,aggregate_feasible=aggregate,generators=items,domain_status=domain,checked_at=datetime.now(timezone.utc))
            out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.SUCCESS); db.session.commit(); return out
        except Exception:
            db.session.rollback(); raise

# ---------------- Tool 07 ----------------

TOOL_07_NAME="check_battery_constraints"

def check_battery_constraints(payload, *, db, models, run_id=None):
    request=payload if isinstance(payload,PlanValidationRequest) else PlanValidationRequest.model_validate(payload)
    Plan=models["Plan"]; Battery=models["Battery"]; Snapshot=models["SystemSnapshot"]
    AgentRunTrace=models["AgentRunTrace"]; ToolCallLog=models["ToolCallLog"]
    with tool_call(db,AgentRunTrace,ToolCallLog,tool_name=TOOL_07_NAME,tool_category="VALIDATION",input_payload=request.model_dump(mode="json"),run_id=run_id) as (rid,log):
        try:
            plan=db.session.get(Plan, request.plan_id)
            if plan is None: raise ValueError(f"Unknown plan_id: {request.plan_id}")
            snapshot_id = request.context_snapshot_id or plan.snapshot_id
            snapshot=db.session.get(Snapshot, snapshot_id)
            if snapshot is None: raise ValueError("Validation snapshot is missing")
            historical={str(x.get("id")):x for x in (snapshot.state_json or {}).get("batteries",[]) if x.get("id") is not None}
            dispatch=plan_dispatch_from_record(plan,PlanDispatch); items=[]; any_unknown=False; any_violation=False
            dt_h=dispatch.interval_minutes/60.0
            batteries = Battery.query.order_by(Battery.id).all()
            known_battery_ids = {str(b.id) for b in batteries}
            referenced_battery_ids = {str(bid) for iv in dispatch.intervals for bid in iv.batteries.keys()}
            unknown_battery_ids = sorted(referenced_battery_ids - known_battery_ids)
            for bid in unknown_battery_ids:
                items.append(BatteryValidationItem(
                    battery_id=bid,
                    soc_path_pct=[None] * len(dispatch.intervals),
                    requested_power_mw=None,
                    soc_before_pct=None,
                    soc_after_pct=None,
                    feasible=False,
                    violations=[ConstraintViolation(
                        resource="battery", resource_id=bid, constraint="UNKNOWN_RESOURCE",
                        message=f"Plan references battery {bid}, but it does not exist in the authoritative battery registry",
                    )],
                ))
                any_violation=True
            for b in batteries:
                hs=historical.get(str(b.id)); violations=[]; soc_path=[]; unknown=False; first_power=None
                if hs is None:
                    items.append(BatteryValidationItem(battery_id=str(b.id),soc_path_pct=[None]*len(dispatch.intervals),requested_power_mw=None,soc_before_pct=None,soc_after_pct=None,feasible=None,violations=[ConstraintViolation(resource="battery",resource_id=str(b.id),constraint="HISTORICAL_STATE",message="Historical battery state is missing")]))
                    any_unknown=True; continue
                vals=(hs.get("soc_pct"),hs.get("capacity_mwh"),hs.get("min_soc_pct"),hs.get("max_soc_pct"),hs.get("max_charge_mw"),hs.get("max_discharge_mw"),hs.get("efficiency"),hs.get("availability_status"))
                if any(v is None for v in vals) or hs.get("capacity_mwh",0)<=0 or hs.get("efficiency",0)<=0 or hs.get("efficiency",0)>1:
                    items.append(BatteryValidationItem(battery_id=str(b.id),soc_path_pct=[None]*len(dispatch.intervals),requested_power_mw=None,soc_before_pct=hs.get("soc_pct"),soc_after_pct=None,feasible=None,violations=[ConstraintViolation(resource="battery",resource_id=str(b.id),constraint="DATA_MISSING_OR_INVALID",message="Battery physical data required for validation is missing or invalid")]))
                    any_unknown=True; continue
                soc=float(hs["soc_pct"]); cap=float(hs["capacity_mwh"]); eff=float(hs["efficiency"])
                for iv in dispatch.intervals:
                    point=iv.batteries.get(str(b.id)); power=None if point is None else point.power_mw
                    if first_power is None: first_power=power
                    if power is None:
                        # Once the trajectory becomes unknown, later SOC values cannot be
                        # reconstructed safely without a new authoritative SOC anchor.
                        unknown=True
                        soc_path.append(None)
                        soc=None
                        continue
                    if soc is None:
                        unknown=True
                        soc_path.append(None)
                        continue
                    ok=True
                    if str(hs["availability_status"]).upper()!="AVAILABLE" and abs(power)>1e-9:
                        ok=False; violations.append(ConstraintViolation(resource="battery",resource_id=str(b.id),interval_index=iv.index,constraint="availability",observed=hs["availability_status"],requested=power,limit="AVAILABLE",message=f"Battery {b.id} is unavailable"))
                    if power < -float(hs["max_charge_mw"])-1e-9:
                        ok=False; violations.append(ConstraintViolation(resource="battery",resource_id=str(b.id),interval_index=iv.index,constraint="max_charge_mw",observed=power,requested=power,limit=-float(hs["max_charge_mw"]),excess=(-float(hs["max_charge_mw"])-power),message=f"Battery {b.id} charge power limit exceeded"))
                    if power > float(hs["max_discharge_mw"])+1e-9:
                        ok=False; violations.append(ConstraintViolation(resource="battery",resource_id=str(b.id),interval_index=iv.index,constraint="max_discharge_mw",observed=power,requested=power,limit=float(hs["max_discharge_mw"]),excess=power-float(hs["max_discharge_mw"]),message=f"Battery {b.id} discharge power limit exceeded"))
                    energy=cap*soc/100.0
                    if power>0: e_next=energy-power*dt_h/eff
                    elif power<0: e_next=energy+(-power)*dt_h*eff
                    else: e_next=energy
                    next_soc=100.0*e_next/cap; soc_path.append(next_soc)
                    if next_soc<float(hs["min_soc_pct"])-1e-9:
                        ok=False; violations.append(ConstraintViolation(resource="battery",resource_id=str(b.id),interval_index=iv.index,constraint="min_soc_pct",observed=next_soc,requested=power,limit=float(hs["min_soc_pct"]),excess=float(hs["min_soc_pct"])-next_soc,message=f"Battery {b.id} SOC below minimum"))
                    if next_soc>float(hs["max_soc_pct"])+1e-9:
                        ok=False; violations.append(ConstraintViolation(resource="battery",resource_id=str(b.id),interval_index=iv.index,constraint="max_soc_pct",observed=next_soc,requested=power,limit=float(hs["max_soc_pct"]),excess=next_soc-float(hs["max_soc_pct"]),message=f"Battery {b.id} SOC above maximum"))
                    soc=next_soc
                feasible=None if unknown and not violations else False if violations else True
                any_unknown |= unknown; any_violation |= bool(violations)
                items.append(BatteryValidationItem(battery_id=str(b.id),soc_path_pct=soc_path,requested_power_mw=first_power,soc_before_pct=hs["soc_pct"],soc_after_pct=(soc if not unknown else None),feasible=feasible,violations=violations))
            aggregate=False if any_violation else None if any_unknown else True
            domain=DomainStatus.INFEASIBLE if any_violation else DomainStatus.UNKNOWN if any_unknown else DomainStatus.FEASIBLE
            result=BatteryValidationResult(plan_id=plan.id,aggregate_feasible=aggregate,batteries=items,domain_status=domain,checked_at=datetime.now(timezone.utc))
            out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.SUCCESS); db.session.commit(); return out
        except Exception:
            db.session.rollback(); raise

# ---------------- Tool 08 ----------------

TOOL_08_NAME="run_power_flow"

# Deliberately empty: project configuration must register an approved benchmark.
# Tests may inject a configured case through this registry without changing the public interface.
CONFIGURED_NETWORK_CASES={}

def _case(network_id):
    # Load the project-owned benchmark lazily so importing the tools does not
    # require pandapower in environments that only run non-network tools.
    if network_id not in CONFIGURED_NETWORK_CASES:
        try:
            from network.solargrid_demo_benchmark import load_network_cases
            CONFIGURED_NETWORK_CASES.update(load_network_cases())
        except Exception:
            pass
    return CONFIGURED_NETWORK_CASES.get(network_id)

def run_power_flow(payload,*,db,models,run_id=None):
    request=payload if isinstance(payload,PowerFlowRequest) else PowerFlowRequest.model_validate(payload)
    Plan=models["Plan"]; AgentRunTrace=models["AgentRunTrace"]; ToolCallLog=models["ToolCallLog"]; Forecast=models["Forecast"]
    with tool_call(db,AgentRunTrace,ToolCallLog,tool_name=TOOL_08_NAME,tool_category="SIMULATION",input_payload=request.model_dump(mode="json"),run_id=run_id) as (rid,log):
        plan=db.session.get(Plan, request.plan_id)
        if plan is None: raise ValueError(f"Unknown plan_id: {request.plan_id}")
        Snapshot=models["SystemSnapshot"]
        validation_snapshot_id=request.context_snapshot_id or plan.snapshot_id
        context_snapshot=db.session.get(Snapshot, validation_snapshot_id)
        if context_snapshot is None:
            result=PowerFlowResult(plan_id=plan.id,network_id=request.network_id,status=PowerFlowStatus.FAILED,converged=None,is_network_feasible=None,solver_errors=[f"Validation snapshot {validation_snapshot_id} does not exist."])
            out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.FAILED); db.session.commit(); return out
        if not request.network_id:
            result=PowerFlowResult(plan_id=plan.id,network_id=None,status=PowerFlowStatus.NOT_CONFIGURED,converged=None,is_network_feasible=None,solver_warnings=["No network case identifier was configured."])
            out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.REJECTED); db.session.commit(); return out
        case=_case(request.network_id)
        if case is None:
            result=PowerFlowResult(plan_id=plan.id,network_id=request.network_id,status=PowerFlowStatus.NOT_CONFIGURED,converged=None,is_network_feasible=None,solver_warnings=["Configured benchmark network case is unavailable."])
            out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.REJECTED); db.session.commit(); return out
        try:
            import pandapower as pp
        except ImportError:
            result=PowerFlowResult(plan_id=plan.id,network_id=request.network_id,status=PowerFlowStatus.FAILED,converged=None,is_network_feasible=None,solver_errors=["pandapower is not installed in the runtime environment."])
            out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.FAILED,"pandapower dependency unavailable"); db.session.commit(); return out
        net=copy.deepcopy(case)
        dispatch=plan_dispatch_from_record(plan,PlanDispatch)
        validation_horizon = dispatch.interval_minutes * len(dispatch.intervals)
        dynamic_pricing_result = _resolve_dynamic_pricing_context_ref(
            dispatch.dynamic_pricing_context,
            expected_snapshot_id=validation_snapshot_id,
            expected_horizon_minutes=validation_horizon,
            db=db,
            ToolCallLog=ToolCallLog,
            snapshot=context_snapshot,
            interval_minutes=dispatch.interval_minutes,
            consumer_name="Tool 08",
        )
        validated_demand_profile = (
            _dynamic_pricing_effective_demand_profile(
                dispatch=dispatch,
                snapshot=context_snapshot,
                Forecast=Forecast,
                dynamic_pricing_result=dynamic_pricing_result,
            )
            if dynamic_pricing_result is not None
            else []
        )
        # Approved case objects must expose explicit mappings. No DB/name guessing is allowed.
        mapping=getattr(net,"solargrid_mapping",None)
        if not isinstance(mapping,dict):
            result=PowerFlowResult(plan_id=plan.id,network_id=request.network_id,status=PowerFlowStatus.FAILED,converged=None,is_network_feasible=None,solver_errors=["Configured network case lacks an approved SolarGrid element mapping."])
            out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.FAILED); db.session.commit(); return out
        voltage_limits=mapping.get("voltage_limits_pu")
        loading_limit=mapping.get("loading_limit_pct")
        if voltage_limits is None or loading_limit is None:
            result=PowerFlowResult(plan_id=plan.id,network_id=request.network_id,status=PowerFlowStatus.FAILED,converged=None,is_network_feasible=None,solver_errors=["Configured network case does not define approved voltage/loading limits."])
            out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.FAILED); db.session.commit(); return out

        mapped_generators = {str(k) for k in (mapping.get("generators") or {}).keys()}
        mapped_batteries = {str(k) for k in (mapping.get("batteries") or {}).keys()}
        referenced_generators = {str(gid) for iv in dispatch.intervals for gid in iv.generators.keys()}
        referenced_batteries = {str(bid) for iv in dispatch.intervals for bid in iv.batteries.keys()}
        missing_generator_mappings = sorted(referenced_generators - mapped_generators)
        missing_battery_mappings = sorted(referenced_batteries - mapped_batteries)
        if missing_generator_mappings or missing_battery_mappings:
            details=[]
            if missing_generator_mappings:
                details.append("generator IDs without approved network mapping: " + ", ".join(missing_generator_mappings))
            if missing_battery_mappings:
                details.append("battery IDs without approved network mapping: " + ", ".join(missing_battery_mappings))
            result=PowerFlowResult(
                plan_id=plan.id, network_id=request.network_id, status=PowerFlowStatus.FAILED,
                converged=None, is_network_feasible=None,
                solver_errors=["Configured network case does not cover every dispatch resource; " + "; ".join(details)],
            )
            out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.FAILED,"incomplete approved network mapping"); db.session.commit(); return out
        interval_results=[]
        all_voltage_violations=[]
        all_thermal_violations=[]
        try:
            for interval_index, interval in enumerate(dispatch.intervals):
                for gid,point in interval.generators.items():
                    element=mapping.get("generators",{}).get(str(gid))
                    if element is not None and hasattr(net,"sgen") and element < len(net.sgen): net.sgen.at[element,"p_mw"]=point.output_mw or 0.0
                    elif element is not None and hasattr(net,"gen") and element < len(net.gen): net.gen.at[element,"p_mw"]=point.output_mw or 0.0
                for bid,point in interval.batteries.items():
                    element=mapping.get("batteries",{}).get(str(bid))
                    if element is not None and hasattr(net,"storage") and element < len(net.storage):
                        # SolarGrid convention: positive battery power = discharge.
                        # pandapower storage uses the consumer convention: positive = charge,
                        # negative = discharge. Invert the sign at this integration boundary.
                        net.storage.at[element,"p_mw"]=-(point.power_mw or 0.0)
                    elif element is not None and hasattr(net,"sgen") and element < len(net.sgen):
                        # sgen uses positive active-power injection, matching SolarGrid discharge.
                        net.sgen.at[element,"p_mw"]=point.power_mw or 0.0
                if mapping.get("loads"):
                    # Keep network validation tied to the same authoritative
                    # SystemSnapshot as Tools 06/07/09. The benchmark mapping
                    # may contain nominal load values, but a validation run must
                    # represent the current snapshot demand rather than silently
                    # validating against a stale 50 MW benchmark load.
                    load_items=list(mapping["loads"].items())
                    snapshot_demand=(
                        validated_demand_profile[interval_index]
                        if dynamic_pricing_result is not None
                        else context_snapshot.demand_mw
                    )
                    configured=[]
                    for lid,load_cfg in load_items:
                        element=int(load_cfg["element"]) if isinstance(load_cfg,dict) else int(load_cfg)
                        nominal=load_cfg.get("p_mw") if isinstance(load_cfg,dict) else None
                        configured.append((element, None if nominal is None else float(nominal)))
                    if snapshot_demand is not None and configured:
                        nominal_total=sum(v for _,v in configured if v is not None and v>0)
                        if nominal_total>0:
                            for element,nominal in configured:
                                share=(nominal or 0.0)/nominal_total
                                net.load.at[element,"p_mw"]=float(snapshot_demand)*share
                        elif len(configured)==1:
                            net.load.at[configured[0][0],"p_mw"]=float(snapshot_demand)
                    else:
                        for element,nominal in configured:
                            if nominal is not None:
                                net.load.at[element,"p_mw"]=nominal
                pp.runpp(net,init="auto")
                converged=bool(net.converged)
                interval_voltage_violations=[]
                interval_thermal_violations=[]
                if converged:
                    for idx,row in net.res_bus.iterrows():
                        vm=float(row.vm_pu)
                        if vm < float(voltage_limits[0]) or vm > float(voltage_limits[1]):
                            interval_voltage_violations.append({"interval_index":interval_index,"bus_index":int(idx),"vm_pu":vm})
                    if hasattr(net,"res_line"):
                        for idx,row in net.res_line.iterrows():
                            loading=float(row.loading_percent)
                            if loading > float(loading_limit):
                                interval_thermal_violations.append({"interval_index":interval_index,"element_type":"line","line_index":int(idx),"loading_pct":loading})
                    if hasattr(net,"res_trafo"):
                        for idx,row in net.res_trafo.iterrows():
                            loading=float(row.loading_percent)
                            if loading > float(loading_limit):
                                interval_thermal_violations.append({"interval_index":interval_index,"element_type":"transformer","transformer_index":int(idx),"loading_pct":loading})
                all_voltage_violations.extend(interval_voltage_violations)
                all_thermal_violations.extend(interval_thermal_violations)
                interval_results.append({"interval_index":interval_index,"converged":converged,"voltage_violation_count":len(interval_voltage_violations),"thermal_violation_count":len(interval_thermal_violations)})
        except Exception as exc:
            result=PowerFlowResult(plan_id=plan.id,network_id=request.network_id,status=PowerFlowStatus.NON_CONVERGED,converged=False,is_network_feasible=None,solver_errors=[str(exc)])
            out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.PARTIAL); db.session.commit(); return out
        bus=[]; vv=[]
        for idx,row in net.res_bus.iterrows():
            item={"bus_index":int(idx),"vm_pu":float(row.vm_pu)}; bus.append(item)
            if item["vm_pu"]<float(voltage_limits[0]) or item["vm_pu"]>float(voltage_limits[1]): vv.append(item)
        lines=[]; thermal=[]
        if hasattr(net,"res_line"):
            for idx,row in net.res_line.iterrows():
                item={"line_index":int(idx),"loading_pct":float(row.loading_percent)}; lines.append(item)
                if item["loading_pct"]>float(loading_limit): thermal.append(item)
        traf=[]
        if hasattr(net,"res_trafo"):
            for idx,row in net.res_trafo.iterrows():
                item={"transformer_index":int(idx),"loading_pct":float(row.loading_percent)}; traf.append(item)
                if item["loading_pct"]>float(loading_limit): thermal.append(item)
        feasible=not all_voltage_violations and not all_thermal_violations
        warnings=[f"Validated {len(interval_results)} dispatch intervals with per-interval constraint checks."]
        if dynamic_pricing_result is not None:
            warnings.append(
                f"Dynamic Pricing ToolCallLog {dispatch.dynamic_pricing_context.tool_call_id} effective demand was used for every network-validation interval."
            )
        result=PowerFlowResult(plan_id=plan.id,network_id=request.network_id,status=PowerFlowStatus.CONVERGED,converged=all(x["converged"] for x in interval_results),is_network_feasible=feasible,bus_voltage_results=bus,line_loading_results=lines,transformer_loading_results=traf,thermal_violations=all_thermal_violations,voltage_violations=all_voltage_violations,congestion=all_thermal_violations,validated_demand_mw=validated_demand_profile,dynamic_pricing_tool_call_id=(dispatch.dynamic_pricing_context.tool_call_id if dynamic_pricing_result is not None else None),solver_warnings=warnings)
        out=result.model_dump(mode="json"); finish_log(log,out,ToolStatus.SUCCESS); db.session.commit(); return out

# ---------------- Tool 09 ----------------

TOOL_09_NAME="evaluate_plans"

def _cost(plan,generators,snapshot,policy):
    dispatch=plan_dispatch_from_record(plan,PlanDispatch); dt=dispatch.interval_minutes/60.0
    model=policy.get("cost_model",{})
    raw_costs=model.get("generation_cost_usd_per_mwh") or model.get("fuel_cost_usd_per_mwh",{})
    costs={str(k):float(v) for k,v in raw_costs.items()} if isinstance(raw_costs,dict) else {}
    wear=model.get("ramping_wear_usd_per_mw_ramp")
    if wear is None:
        return None

    # Policy cost keys are canonical Generator.name values (the six project identities),
    # while DB primary keys are internal row IDs. Resolve by the explicit
    # Generator.name field; never assume that a DB ID equals a policy key.
    generator_costs={}
    for g in generators:
        name=str(g.name) if g.name is not None else None
        if name and name in costs:
            generator_costs[str(g.id)]=costs[name]
        elif str(g.id) in costs:
            # Backward-compatible support for older test/demo policies that
            # keyed costs by DB ID. Older policies may use DB IDs; the current demo policy uses project names.
            generator_costs[str(g.id)]=costs[str(g.id)]
        else:
            return None

    historical={str(x.get("id")):x for x in (snapshot.state_json or {}).get("generators",[]) if x.get("id") is not None}
    prev={str(g.id):(historical.get(str(g.id)) or {}).get("current_output_mw") for g in generators}
    total=0.0
    # Historical previous output comes from the plan's snapshot through Tool 06.
    for iv in dispatch.intervals:
        for g in generators:
            p=iv.generators.get(str(g.id))
            if p is None or p.output_mw is None:
                return None
            if prev[str(g.id)] is None:
                return None
            total += float(p.output_mw)*dt*generator_costs[str(g.id)]
            total += abs(float(p.output_mw)-prev[str(g.id)])*float(wear)
            prev[str(g.id)]=float(p.output_mw)
    return total

def _residual_metrics(plan):
    d=plan_dispatch_from_record(plan,PlanDispatch); vals=[iv.residual_imbalance_mw for iv in d.intervals]
    if any(v is None for v in vals): return None,None
    worst=max(vals,key=lambda v:abs(float(v))); return max(abs(float(v)) for v in vals),float(worst)

def _structured_reasons(violations):
    return [f"{v.get('constraint')}; interval={v.get('interval_index')}; requested={v.get('requested')}; limit={v.get('limit')}; excess={v.get('excess')}" for v in violations]

def evaluate_plans(payload,*,db,models,run_id=None):
    request=payload if isinstance(payload,PlanEvaluationRequest) else PlanEvaluationRequest.model_validate(payload)
    Plan=models["Plan"]; Generator=models["Generator"]; ReserveAssessment=models["ReserveAssessment"]; Snapshot=models["SystemSnapshot"]; Forecast=models["Forecast"]
    AgentRunTrace=models["AgentRunTrace"]; ToolCallLog=models["ToolCallLog"]; PlanEvaluation=models["PlanEvaluation"]
    with tool_call(db,AgentRunTrace,ToolCallLog,tool_name=TOOL_09_NAME,tool_category="POLICY",input_payload=request.model_dump(mode="json"),run_id=run_id) as (rid,log):
        try:
            policy=load_decision_policy(request.decision_policy_version)
            if not _policy_status(policy):
                raise ValueError("Decision policy is not approved")
            network_required=bool(policy.get("network_validation_required"))
            tolerance=policy.get("hard_constraints",{}).get("residual_imbalance_tolerance_mw")
            if tolerance is None:
                raise ValueError("SPECIFICATION ISSUE: residual imbalance tolerance is not configured")

            plans=Plan.query.filter(Plan.id.in_(request.plan_ids)).order_by(Plan.id).all()
            found={p.id for p in plans}
            missing=[x for x in request.plan_ids if x not in found]
            if missing:
                raise ValueError(f"Unknown plan_ids: {missing}")

            generators=Generator.query.order_by(Generator.id).all()
            evaluations=[]
            feasible=[]

            for plan in plans:
                # Tool 09 is the final deterministic gate. It must evaluate with
                # the same authoritative context used by Tools 06-08, not silently
                # rebuild a different one. For normal candidate evaluation the
                # context snapshot is the plan snapshot; revalidation may provide a
                # newer explicit snapshot.
                validation_snapshot_id = request.context_snapshot_id or plan.snapshot_id
                validation_snapshot = db.session.get(Snapshot, validation_snapshot_id)
                if validation_snapshot is None:
                    raise ValueError(f"Validation snapshot {validation_snapshot_id} does not exist")

                dispatch=plan_dispatch_from_record(plan,PlanDispatch)
                derived_horizon=dispatch.interval_minutes*len(dispatch.intervals)
                validation_horizon=request.horizon_minutes or derived_horizon
                if validation_horizon != derived_horizon:
                    raise ValueError(
                        f"Tool 09 horizon mismatch for plan {plan.id}: request={validation_horizon}, dispatch={derived_horizon}"
                    )

                dynamic_pricing_result = _resolve_dynamic_pricing_context_ref(
                    dispatch.dynamic_pricing_context,
                    expected_snapshot_id=validation_snapshot_id,
                    expected_horizon_minutes=validation_horizon,
                    db=db,
                    ToolCallLog=ToolCallLog,
                    snapshot=validation_snapshot,
                    interval_minutes=dispatch.interval_minutes,
                    consumer_name="Tool 09",
                )
                validated_demand_profile = (
                    _dynamic_pricing_effective_demand_profile(
                        dispatch=dispatch,
                        snapshot=validation_snapshot,
                        Forecast=Forecast,
                        dynamic_pricing_result=dynamic_pricing_result,
                    )
                    if dynamic_pricing_result is not None
                    else []
                )

                network_id=request.network_id or policy.get("network_id")
                if network_required and not network_id:
                    raise ValueError("Approved policy requires network validation but no network_id is available")

                gen=check_generator_constraints(
                    {"plan_id":plan.id,"context_snapshot_id":validation_snapshot_id},
                    db=db,models=models,run_id=rid,
                )
                bat=check_battery_constraints(
                    {"plan_id":plan.id,"context_snapshot_id":validation_snapshot_id},
                    db=db,models=models,run_id=rid,
                )
                pf=run_power_flow(
                    {"plan_id":plan.id,"network_id":network_id,"context_snapshot_id":validation_snapshot_id},
                    db=db,models=models,run_id=rid,
                )

                if dynamic_pricing_result is not None:
                    expected_tool_call_id = dispatch.dynamic_pricing_context.tool_call_id
                    if pf.get("status") == PowerFlowStatus.CONVERGED.value:
                        if pf.get("dynamic_pricing_tool_call_id") != expected_tool_call_id:
                            raise ValueError(
                                f"Tool 08 Dynamic Pricing provenance mismatch for plan {plan.id}"
                            )
                        observed_profile = [float(x) for x in (pf.get("validated_demand_mw") or [])]
                        if len(observed_profile) != len(validated_demand_profile) or any(
                            abs(a-b) > 1e-9 for a,b in zip(observed_profile, validated_demand_profile)
                        ):
                            raise ValueError(
                                f"Tool 08 effective demand profile does not match Tool 09 for plan {plan.id}"
                            )

                violations=[]
                for item in gen.get("generators",[]):
                    violations.extend(item.get("violations",[]))
                for item in bat.get("batteries",[]):
                    violations.extend(item.get("violations",[]))

                pf_feasible=pf.get("is_network_feasible")
                if network_required:
                    if pf_feasible is False:
                        violations.append(ConstraintViolation(
                            resource="network", resource_id=network_id,
                            constraint="network_feasibility", observed=False, requested=True, limit=True,
                            message="Configured network validation reports the plan as infeasible"
                        ))
                    elif pf_feasible is None:
                        violations.append(ConstraintViolation(
                            resource="network", resource_id=network_id,
                            constraint="network_feasibility", observed="UNKNOWN", requested=True,
                            limit=True, message="Network feasibility could not be established"
                        ))
                    for v in pf.get("thermal_violations",[]) + pf.get("voltage_violations",[]) + pf.get("congestion",[]):
                        violations.append(ConstraintViolation(
                            resource="network", resource_id=network_id,
                            constraint=str(v.get("constraint", v.get("type", "network_violation"))),
                            observed=v.get("observed"), requested=v.get("requested"),
                            limit=v.get("limit"), excess=v.get("excess"),
                            message=str(v.get("message", "Network validation violation"))
                        ))

                genf=gen.get("aggregate_feasible")
                batf=bat.get("aggregate_feasible")
                residual_abs,residual_signed=_residual_metrics(plan)

                reserve=None
                if request.reserve_assessment_id is not None:
                    reserve_candidate=db.session.get(ReserveAssessment,request.reserve_assessment_id)
                    if reserve_candidate is None:
                        raise ValueError(f"ReserveAssessment {request.reserve_assessment_id} does not exist")
                    # A supplied ID is authoritative and must match the exact
                    # validation context; silently discarding it is what caused
                    # false UNKNOWN results in the previous workflow.
                    if int(reserve_candidate.snapshot_id) != int(validation_snapshot_id):
                        raise ValueError(
                            f"ReserveAssessment {reserve_candidate.id} belongs to snapshot {reserve_candidate.snapshot_id}, "
                            f"not validation snapshot {validation_snapshot_id}"
                        )
                    if reserve_candidate.time_horizon_minutes is None or abs(float(reserve_candidate.time_horizon_minutes)-float(validation_horizon))>1e-9:
                        raise ValueError(
                            f"ReserveAssessment {reserve_candidate.id} horizon does not match {validation_horizon} minutes"
                        )
                    if reserve_candidate.required_reserve_mw is None or reserve_candidate.actual_reserve_mw is None:
                        raise ValueError(f"ReserveAssessment {reserve_candidate.id} is incomplete")
                    reserve=reserve_candidate
                else:
                    reserve_candidates=(
                        ReserveAssessment.query
                        .filter_by(snapshot_id=validation_snapshot_id)
                        .order_by(ReserveAssessment.id.desc())
                        .all()
                    )
                    reserve=next((
                        item for item in reserve_candidates
                        if item.time_horizon_minutes is not None
                        and abs(float(item.time_horizon_minutes)-float(validation_horizon))<1e-9
                        and item.required_reserve_mw is not None
                        and item.actual_reserve_mw is not None
                    ),None)

                reserve_margin=None if reserve is None else reserve.actual_reserve_mw-reserve.required_reserve_mw

                pf_status=NetworkValidationStatus.NOT_EVALUATED
                pff=pf.get("is_network_feasible")
                if pf.get("status")=="CONVERGED":
                    pf_status=NetworkValidationStatus.PASS if pff is True else NetworkValidationStatus.FAIL
                elif pf.get("status")=="NOT_CONFIGURED":
                    pf_status=NetworkValidationStatus.UNKNOWN if network_required else NetworkValidationStatus.NOT_EVALUATED
                    pff=None
                else:
                    pf_status=NetworkValidationStatus.UNKNOWN
                    pff=None

                hard=[]
                unknown=[]
                if genf is False:
                    hard += _structured_reasons([v for i in gen.get("generators",[]) for v in i.get("violations",[])])
                elif genf is None:
                    unknown.append("generator feasibility UNKNOWN")
                if batf is False:
                    hard += _structured_reasons([v for i in bat.get("batteries",[]) for v in i.get("violations",[])])
                elif batf is None:
                    unknown.append("battery feasibility UNKNOWN")
                if residual_abs is None:
                    unknown.append("residual imbalance UNKNOWN")
                elif residual_abs>float(tolerance)+1e-9:
                    hard.append(f"residual_imbalance; interval=worst; requested={residual_signed}; limit={tolerance}; excess={residual_abs-float(tolerance)}")
                if reserve is None:
                    unknown.append(
                        f"reserve evidence UNKNOWN for snapshot={validation_snapshot_id}, horizon={validation_horizon}"
                    )
                elif reserve.actual_reserve_mw+1e-9<reserve.required_reserve_mw:
                    hard.append(f"reserve; interval=0; requested={reserve.actual_reserve_mw}; limit={reserve.required_reserve_mw}; excess={reserve.required_reserve_mw-reserve.actual_reserve_mw}")
                if network_required:
                    if pff is False:
                        hard.extend([f"network; interval={v.get('line_index',v.get('bus_index',v.get('transformer_index','unknown')))}; requested={v.get('loading_pct',v.get('vm_pu'))}; limit=approved-network-limit; excess=unknown" for v in (pf.get("thermal_violations",[])+pf.get("voltage_violations",[]))] or ["network feasibility violation evidenced by Tool 08"])
                    elif pff is None:
                        errors=pf.get("solver_errors") or pf.get("solver_warnings") or []
                        detail=f": {' | '.join(map(str,errors))}" if errors else ""
                        unknown.append(f"network feasibility UNKNOWN{detail}")

                cost=_cost(plan,generators,validation_snapshot,policy)
                if cost is None:
                    unknown.append("configured cost evidence UNKNOWN")

                if hard:
                    feasible_state=False; domain=DomainStatus.INFEASIBLE; rejection="; ".join(hard)
                elif unknown:
                    feasible_state=None; domain=DomainStatus.UNKNOWN; rejection="; ".join(unknown)
                else:
                    feasible_state=True; domain=DomainStatus.FEASIBLE; rejection=None

                if feasible_state is True:
                    feasible.append((plan,residual_abs,reserve_margin,cost))

                violation_payload=[
                    v.model_dump(mode="json") if isinstance(v,ConstraintViolation)
                    else ConstraintViolation.model_validate(v).model_dump(mode="json")
                    for v in violations
                ]
                pe=PlanEvaluation(
                    plan_id=plan.id,is_feasible=feasible_state,constraint_violations=violation_payload,
                    power_flow_result=pf,reserve_margin_mw=reserve_margin,imbalance_mw=residual_signed,
                    estimated_cost=cost,rejection_reason=rejection,decision_policy_version=request.decision_policy_version
                )
                db.session.add(pe)
                evaluations.append(PlanEvaluationResult(
                    plan_id=plan.id,hard_feasible=feasible_state,is_feasible=feasible_state,
                    violations=[ConstraintViolation.model_validate(v) for v in violations],
                    power_flow_feasible=pff,network_validation_status=pf_status,reserve_margin_mw=reserve_margin,
                    residual_imbalance_mw=residual_signed,imbalance_mw=residual_signed,estimated_cost=cost,
                    validated_demand_mw=validated_demand_profile,
                    dynamic_pricing_tool_call_id=(dispatch.dynamic_pricing_context.tool_call_id if dynamic_pricing_result is not None else None),
                    tradeoffs=[],rejection_reason=rejection,decision_policy_version=request.decision_policy_version
                ))

            feasible.sort(key=lambda x:(x[1],-(x[2] if x[2] is not None else float("-inf")),x[3],x[0].id))
            selected=feasible[0][0] if feasible else None
            result=EvaluatePlansResult(
                evaluations=evaluations,
                decision_summary={
                    "feasible_plan_ids":[x[0].id for x in feasible],
                    "selected_plan_id":selected.id if selected else None,
                    "validation_context":{
                        "snapshot_id":request.context_snapshot_id,
                        "reserve_assessment_id":request.reserve_assessment_id,
                        "network_id":request.network_id or policy.get("network_id"),
                        "horizon_minutes":request.horizon_minutes,
                        "dynamic_pricing_plan_ids":[
                            item.plan_id for item in evaluations if item.dynamic_pricing_tool_call_id is not None
                        ],
                    },
                },
                selected_plan_id=selected.id if selected else None,
                selection_reason="Lexicographic policy: minimum absolute residual imbalance, maximum reserve margin, minimum estimated cost, lowest plan_id." if selected else None,
                domain_status=DomainStatus.FEASIBLE if selected else DomainStatus.UNKNOWN if any(x.hard_feasible is None for x in evaluations) else DomainStatus.INFEASIBLE,
            )
            out=result.model_dump(mode="json")
            finish_log(log,out,ToolStatus.SUCCESS)
            db.session.commit()
            return out
        except Exception:
            db.session.rollback(); raise
