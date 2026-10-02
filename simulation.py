"""Deterministic simulated world for SOLARGRID AI.

This module is deliberately not a planner and never approves/selects plans.
Tool 13 is the safety gate; this module only advances the simulated world,
applies an already-approved dispatch, and persists synthetic observations.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import random
from pathlib import Path
from typing import Any

from env_config import PROJECT_ROOT, load_project_env

load_project_env()

SIMULATION_STUB_NOTICE = "COURSE DEMO SYNTHETIC SIMULATION ONLY"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "simulation_v1.json"


class SimulationClock:
    """Controllable deterministic simulation clock."""

    def __init__(self, current_time: datetime | None = None, seed: int = 42):
        self.current_time = current_time
        self.seed = seed
        self.rng = random.Random(seed)

    def set(self, value: datetime) -> datetime:
        self.current_time = value
        return value

    def advance(self, minutes: float) -> datetime:
        if self.current_time is None:
            raise ValueError("Simulation clock has not been initialized.")
        if minutes < 0:
            raise ValueError("Simulation clock cannot move backwards.")
        self.current_time += timedelta(minutes=float(minutes))
        return self.current_time


_clock = SimulationClock()


def get_simulation_clock() -> datetime | None:
    return _clock.current_time


def reset_simulation_clock(value: datetime | None = None, seed: int = 42) -> datetime | None:
    global _clock
    _clock = SimulationClock(value, seed)
    return _clock.current_time


def advance_simulation_clock(minutes: float) -> datetime:
    return _clock.advance(minutes)


def _load_config(path: str | None = None) -> dict[str, Any]:
    configured = path or os.getenv("SOLARGRID_SIMULATION_CONFIG")
    config_path = Path(configured).expanduser() if configured else Path(DEFAULT_CONFIG_PATH)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _state_snapshot_payload(generators, batteries) -> dict[str, Any]:
    return {
        "generators": [
            {
                "id": g.id,
                "current_output_mw": g.current_output_mw,
                "min_output_mw": g.min_output_mw,
                "max_output_mw": g.max_output_mw,
                "ramp_rate_mw_per_min": g.ramp_rate_mw_per_min,
                "availability_status": g.availability_status,
                "constraints_json": deepcopy(g.constraints_json),
            }
            for g in generators
        ],
        "batteries": [
            {
                "id": b.id,
                "soc_pct": b.soc_pct,
                "capacity_mwh": b.capacity_mwh,
                "min_soc_pct": b.min_soc_pct,
                "max_soc_pct": b.max_soc_pct,
                "max_charge_mw": b.max_charge_mw,
                "max_discharge_mw": b.max_discharge_mw,
                "efficiency": b.efficiency,
                "current_power_mw": b.current_power_mw,
                "availability_status": b.availability_status,
            }
            for b in batteries
        ],
    }


def _create_snapshot(*, run_id, timestamp, source_snapshot, generators, batteries,
                     demand_mw, solar_gen_mw, other_gen_mw, state_json,
                     reserve_margin_mw=None, data_quality="SIMULATED"):
    from database import SystemSnapshot, db

    battery_soc = None
    socs = [b.soc_pct for b in batteries if b.soc_pct is not None]
    if socs:
        battery_soc = sum(socs) / len(socs)

    snapshot = SystemSnapshot(
        run_id=run_id,
        timestamp=timestamp,
        demand_mw=demand_mw,
        solar_gen_mw=solar_gen_mw,
        other_gen_mw=other_gen_mw,
        battery_soc_pct=battery_soc,
        reserve_margin_mw=reserve_margin_mw,
        grid_status=source_snapshot.grid_status if source_snapshot else "STABLE",
        data_quality=data_quality,
        data_source="SIMULATION",
        is_synthetic=True,
        state_json=deepcopy(state_json),
    )
    db.session.add(snapshot)
    db.session.flush()
    return snapshot


def _latest_forecast(variable: str, target_time: datetime, snapshot_id: int | None = None):
    from database import Forecast

    query = Forecast.query.filter(Forecast.variable_name == variable)
    if snapshot_id is not None:
        query = query.filter(Forecast.snapshot_id == snapshot_id)
    rows = query.order_by(Forecast.target_time.asc(), Forecast.issued_at.desc()).all()
    if not rows:
        return None
    compatible = [r for r in rows if r.target_time == target_time]
    return compatible[-1] if compatible else None


def _create_followup_forecasts(*, post_snapshot, interval_minutes: float, horizon_minutes: float,
                               solar_baseline_mw: float | None = None,
                               demand_baseline_mw: float | None = None) -> list[int]:
    """Create the next planning window for a synthetic post-execution snapshot.

    Tool 05 intentionally requires a complete solar/demand MW forecast window tied
    to the snapshot it is planning from.  A post-execution snapshot is a new grid
    observation, so forecasts owned only by the old planning snapshot cannot be
    silently reused.  For the course simulation we therefore issue a transparent
    *persistence forecast*: the latest simulated solar/demand observation at T+0
    is held constant across the next horizon.

    This does not fabricate telemetry: these rows are explicitly labelled
    ``SIMULATION_PERSISTENCE`` forecasts and remain separate from ActualMeasurement.
    Production deployments should replace this with an approved live forecasting
    integration.
    """
    from database import Forecast, db

    interval = float(interval_minutes)
    horizon = float(horizon_minutes)
    if interval <= 0 or horizon <= 0:
        return []

    # In the solar-only model the aggregate solar forecast and the generator
    # rows describe the same fleet.  The post-execution snapshot therefore stores
    # actual dispatched solar generation directly.  A persistence forecast may
    # safely start from that measured post-execution generation; it must never add
    # generator dispatch as a second generation term.
    solar = _number(solar_baseline_mw)
    demand = _number(demand_baseline_mw)
    if solar is None:
        solar = _number(post_snapshot.solar_gen_mw)
    if demand is None:
        demand = _number(post_snapshot.demand_mw)
    if solar is None or demand is None:
        return []

    # Avoid duplicate rows if this helper is ever retried for the same snapshot.
    existing = Forecast.query.filter_by(snapshot_id=post_snapshot.id).count()
    if existing:
        return [row.id for row in Forecast.query.filter_by(snapshot_id=post_snapshot.id).all()]

    steps = int(round(horizon / interval))
    if abs(steps * interval - horizon) > 1e-9:
        raise ValueError("Follow-up forecast horizon must be divisible by interval_minutes.")

    issued_at = post_snapshot.timestamp
    created_ids: list[int] = []
    for step in range(steps + 1):
        target = post_snapshot.timestamp + timedelta(minutes=step * interval)
        for variable, value in (("solar", solar), ("demand", demand)):
            row = Forecast(
                snapshot_id=post_snapshot.id,
                variable_name=variable,
                forecast_value=value,
                unit="MW",
                source="SIMULATION_PERSISTENCE",
                issued_at=issued_at,
                target_time=target,
                confidence_interval=None,
            )
            db.session.add(row)
            db.session.flush()
            created_ids.append(row.id)
    return created_ids


def _actual_value(forecast_value: float | None, fallback_value: float | None,
                  deviation_pct: float | None) -> float | None:
    """Resolve the simulated actual for one environmental variable.

    The simulation clock advances through the plan horizon.  When no explicit
    deviation is configured, the approved forecast at that simulated timestamp is
    the deterministic actual.  The previous implementation incorrectly kept the
    original snapshot value whenever the forecast moved, which made post-execution
    Demand/Solar remain identical to Before even after 120 simulated minutes.
    """
    if forecast_value is None:
        return fallback_value
    if deviation_pct is None:
        return forecast_value
    return forecast_value * (1.0 + deviation_pct / 100.0)


def _normal_execution_offset(*, enabled: bool, max_abs_mw: float,
                             target_mw: float | None, apply_to_zero_targets: bool) -> float:
    """Return a small deterministic control/telemetry offset for demo execution.

    This is intentionally *not* treated as a partial failure. The configured
    command-tracking tolerance decides whether the resulting actual still tracks
    the approved command acceptably. The seeded simulation RNG keeps repeated
    demos reproducible while avoiding impossible perfect equality everywhere.
    """
    if not enabled or max_abs_mw <= 0 or target_mw is None:
        return 0.0
    if abs(float(target_mw)) <= 1e-12 and not apply_to_zero_targets:
        return 0.0
    magnitude = float(max_abs_mw) * (0.45 + 0.55 * _clock.rng.random())
    sign = -1.0 if _clock.rng.random() < 0.5 else 1.0
    return sign * magnitude

def apply_scenario_to_state(state: dict[str, Any], *, solar_delta_pct: float | None,
                            demand_delta_pct: float | None,
                            generator_outages: list[str], other_changes: dict[str, Any] | None = None) -> dict[str, Any]:
    out = deepcopy(state)
    if solar_delta_pct is not None and out.get("solar_generation_mw") is not None:
        out["solar_generation_mw"] *= 1.0 + solar_delta_pct / 100.0
    if demand_delta_pct is not None and out.get("demand_mw") is not None:
        out["demand_mw"] *= 1.0 + demand_delta_pct / 100.0
    outages = {str(x) for x in generator_outages}
    for gen in out.get("generators", []):
        if str(gen.get("generator_id", gen.get("id"))) in outages:
            gen["availability_status"] = "TRIPPED"
    if other_changes:
        out.setdefault("scenario_other_changes", deepcopy(other_changes))
    return out


def inject_forecast_revision(snapshot_id: int, variable_name: str, forecast_value: float,
                             target_time: datetime, source: str = "SIMULATION_EVENT") -> int:
    from database import Forecast, db
    row = Forecast(snapshot_id=snapshot_id, variable_name=variable_name,
                   forecast_value=forecast_value, unit="MW", source=source,
                   issued_at=_clock.current_time or datetime.now(timezone.utc).replace(tzinfo=None), target_time=target_time)
    db.session.add(row)
    db.session.commit()
    return row.id


def inject_solar_drop(snapshot_id: int, drop_pct: float, target_time: datetime) -> int:
    base = _latest_forecast("solar", target_time, snapshot_id)
    if base is None or base.forecast_value is None:
        raise ValueError("No solar MW forecast exists for the requested target time.")
    return inject_forecast_revision(snapshot_id, "solar", base.forecast_value * (1.0 - drop_pct / 100.0), target_time)


def inject_demand_increase(snapshot_id: int, increase_pct: float, target_time: datetime) -> int:
    base = _latest_forecast("demand", target_time, snapshot_id)
    if base is None or base.forecast_value is None:
        raise ValueError("No demand MW forecast exists for the requested target time.")
    return inject_forecast_revision(snapshot_id, "demand", base.forecast_value * (1.0 + increase_pct / 100.0), target_time)


def inject_generator_trip(generator_id: int, availability_status: str = "TRIPPED") -> dict[str, Any]:
    from database import Generator, db
    generator = db.session.get(Generator, generator_id)
    if generator is None:
        raise ValueError(f"Generator {generator_id} not found.")
    generator.availability_status = availability_status
    db.session.commit()
    return {"generator_id": generator_id, "availability_status": availability_status}


def _aggregate_dynamic_pricing_deltas(entries: list[dict[str, Any]], *, amount_key: str) -> dict[int, float]:
    deltas: dict[int, float] = {}
    for entry in entries:
        amount = float(entry[amount_key])
        source = int(entry["source_interval_index"])
        target = int(entry["target_interval_index"])
        deltas[source] = deltas.get(source, 0.0) - amount
        deltas[target] = deltas.get(target, 0.0) + amount
    return {index: value for index, value in deltas.items() if abs(value) > 1e-12}


def _resolve_dynamic_pricing_execution(plan, actions: dict[str, Any], base_snapshot, *, config: dict[str, Any], options: dict[str, Any]):
    """Resolve the trusted Tool 17 action and build deterministic execution data.

    The persisted Plan stores only ``dynamic_pricing_context.tool_call_id``.
    Execution reloads the Tool 17 record, revalidates provenance/alignment, and
    derives both requested and simulated-actual load shifts from that source.
    No raw load-shift engineering values are accepted from the caller.
    """
    from database import ToolCallLog, db
    from schemas import PlanDispatch
    from tool_common import resolve_dynamic_pricing_context_ref

    dispatch = PlanDispatch.model_validate(actions)
    if dispatch.dynamic_pricing_context is None:
        return None

    horizon_minutes = dispatch.interval_minutes * len(dispatch.intervals)
    result = resolve_dynamic_pricing_context_ref(
        dispatch.dynamic_pricing_context,
        expected_snapshot_id=plan.snapshot_id,
        expected_horizon_minutes=horizon_minutes,
        db=db,
        ToolCallLog=ToolCallLog,
        snapshot=base_snapshot,
        interval_minutes=dispatch.interval_minutes,
        consumer_name="simulation.apply_plan",
    )
    if not result.load_shift_entries or not result.demand_adjustments:
        raise ValueError("Dynamic Pricing plan has no executable conserved load-shift action")

    # Phase 6 execution requires the explicit source->target entries and the
    # aggregated Tool 17 demand deltas to describe the exact same action.
    entry_deltas: dict[int, float] = {}
    for entry in result.load_shift_entries:
        entry_deltas[entry.source_interval_index] = entry_deltas.get(entry.source_interval_index, 0.0) - float(entry.shifted_mw)
        entry_deltas[entry.target_interval_index] = entry_deltas.get(entry.target_interval_index, 0.0) + float(entry.shifted_mw)
    declared_deltas = {item.interval_index: float(item.demand_delta_mw) for item in result.demand_adjustments}
    for index in set(entry_deltas) | set(declared_deltas):
        if abs(entry_deltas.get(index, 0.0) - declared_deltas.get(index, 0.0)) > 1e-6:
            raise ValueError(
                f"Dynamic Pricing load-shift entries do not match demand adjustment at interval {index}"
            )

    dp_cfg = config.get("dynamic_pricing_execution", {}) if isinstance(config, dict) else {}
    response_factor = options.get("dynamic_pricing_response_factor", dp_cfg.get("response_factor", 1.0))
    try:
        response_factor = float(response_factor)
    except (TypeError, ValueError):
        raise ValueError("dynamic_pricing_response_factor must be numeric")
    if response_factor != response_factor or abs(response_factor) == float("inf") or not 0.0 <= response_factor <= 1.0:
        raise ValueError("dynamic_pricing_response_factor must be between 0 and 1")

    requested_entries = [item.model_dump(mode="json") for item in result.load_shift_entries]
    requested_adjustments = [item.model_dump(mode="json") for item in result.demand_adjustments]

    actual_entries: list[dict[str, Any]] = []
    for item in result.load_shift_entries:
        requested_mw = float(item.shifted_mw)
        actual_mw = requested_mw * response_factor
        actual_entries.append({
            "source_interval_index": item.source_interval_index,
            "source_time": item.source_time.isoformat(),
            "target_interval_index": item.target_interval_index,
            "target_time": item.target_time.isoformat(),
            "requested_shifted_mw": requested_mw,
            "actual_shifted_mw": actual_mw,
        })

    actual_deltas = _aggregate_dynamic_pricing_deltas(actual_entries, amount_key="actual_shifted_mw")
    requested_delta_map = {
        int(item.interval_index): float(item.demand_delta_mw)
        for item in result.demand_adjustments
    }

    tolerance = 1e-6
    net_actual = sum(actual_deltas.values())
    actual_shifted_in = sum(max(value, 0.0) for value in actual_deltas.values())
    actual_shifted_out = sum(max(-value, 0.0) for value in actual_deltas.values())
    actual_total = sum(float(item["actual_shifted_mw"]) for item in actual_entries)
    conservation_verified = (
        abs(net_actual) <= tolerance
        and abs(actual_shifted_in - actual_shifted_out) <= tolerance
        and abs(actual_total - actual_shifted_in) <= tolerance
    )
    if not conservation_verified:
        raise ValueError("Dynamic Pricing simulated load shift failed conservation validation")

    actual_adjustments = []
    for index in sorted(actual_deltas):
        interval = dispatch.intervals[index]
        actual_adjustments.append({
            "interval_index": index,
            "target_time": (interval.start + timedelta(minutes=dispatch.interval_minutes)).isoformat(),
            "demand_delta_mw": actual_deltas[index],
        })

    return {
        "tool_call_id": int(dispatch.dynamic_pricing_context.tool_call_id),
        "energy_scope": result.energy_scope,
        "response_factor": response_factor,
        "requested_entries": requested_entries,
        "requested_adjustments": requested_adjustments,
        "requested_delta_by_interval": requested_delta_map,
        "requested_total_shifted_mw": float(result.total_shifted_mw or 0.0),
        "actual_entries": actual_entries,
        "actual_adjustments": actual_adjustments,
        "actual_delta_by_interval": actual_deltas,
        "actual_total_shifted_mw": actual_total,
        "conservation_verified": conservation_verified,
    }


def _requested_for_interval(interval: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    requested = {"generators": [], "batteries": []}
    for gid, point in (interval.get("generators") or {}).items():
        target = point.get("output_mw") if isinstance(point, dict) else None
        requested["generators"].append({"id": gid, "target_mw": target})
    for bid, point in (interval.get("batteries") or {}).items():
        target = point.get("power_mw") if isinstance(point, dict) else None
        requested["batteries"].append({"id": bid, "target_mw": target})
    return requested, {"generators": [], "batteries": []}


def apply_plan(plan_id: int, *, simulation_options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute an approved plan interval-by-interval in the simulated world."""
    from database import Plan, Generator, Battery, SystemSnapshot, ActualMeasurement, db

    options = simulation_options or {}
    config = _load_config(options.get("config_path"))
    seed = int(options.get("seed", config.get("seed", 42)))
    if _clock.current_time is None:
        plan = db.session.get(Plan, plan_id)
        base = db.session.get(SystemSnapshot, plan.snapshot_id) if plan else None
        reset_simulation_clock(base.timestamp if base else datetime.now(timezone.utc).replace(tzinfo=None), seed)
    else:
        _clock.rng.seed(seed)

    plan = db.session.get(Plan, plan_id)
    if not plan:
        return {"execution_status": "FAILED", "requested_actions": {}, "actual_actions": None,
                "deviations": [], "failure_information": "Plan not found."}

    actions = plan.actions if isinstance(plan.actions, dict) else {}
    intervals = actions.get("intervals", [])
    if not intervals:
        return {"execution_status": "FAILED", "requested_actions": actions, "actual_actions": None,
                "deviations": [], "failure_information": "Plan contains no dispatch intervals."}

    base_snapshot = db.session.get(SystemSnapshot, plan.snapshot_id)
    generators = Generator.query.order_by(Generator.id).all()
    batteries = Battery.query.order_by(Battery.id).all()
    if base_snapshot is None:
        return {"execution_status": "FAILED", "requested_actions": actions, "actual_actions": None,
                "deviations": [], "failure_information": "Plan base snapshot not found."}

    try:
        dynamic_pricing_execution = _resolve_dynamic_pricing_execution(
            plan, actions, base_snapshot, config=config, options=options
        )
    except Exception as exc:
        return {
            "execution_status": "FAILED",
            "requested_actions": actions,
            "actual_actions": None,
            "deviations": [],
            "failure_information": f"Dynamic Pricing execution context rejected: {exc}",
            "dynamic_pricing_execution": None,
        }

    # Capture the pre-execution state before any simulated asset mutation.
    pre_state = _state_snapshot_payload(generators, batteries)
    pre_snapshot = _create_snapshot(
        run_id=plan.run_id, timestamp=_clock.current_time, source_snapshot=base_snapshot,
        generators=generators, batteries=batteries,
        demand_mw=base_snapshot.demand_mw, solar_gen_mw=base_snapshot.solar_gen_mw,
        other_gen_mw=base_snapshot.other_gen_mw, state_json=pre_state,
        reserve_margin_mw=base_snapshot.reserve_margin_mw, data_quality="PRE_EXECUTION"
    )

    cfg_failure = config.get("partial_failure", {})
    failure_enabled = bool(options.get("partial_failure_enabled", cfg_failure.get("enabled", False)))
    failure_factor = float(options.get("partial_failure_factor", cfg_failure.get("factor", 0.5)))
    if not 0.0 < failure_factor < 1.0:
        raise ValueError("partial_failure_factor must be between 0 and 1.")

    sim_cfg = config.get("actuals", {})
    solar_dev = options.get("solar_deviation_pct", sim_cfg.get("solar_deviation_pct"))
    demand_dev = options.get("demand_deviation_pct", sim_cfg.get("demand_deviation_pct"))

    variability_cfg = config.get("execution_variability", {})
    variability_enabled = bool(options.get("execution_variability_enabled", variability_cfg.get("enabled", False)))
    generator_variability_mw = max(0.0, float(options.get(
        "generator_variability_max_abs_mw", variability_cfg.get("generator_max_abs_mw", 0.0)
    )))
    battery_variability_mw = max(0.0, float(options.get(
        "battery_variability_max_abs_mw", variability_cfg.get("battery_max_abs_mw", 0.0)
    )))
    variability_zero_targets = bool(options.get(
        "execution_variability_apply_to_zero_targets", variability_cfg.get("apply_to_zero_targets", False)
    ))

    requested_all = {"intervals": []}
    actual_all = {"intervals": [], "normal_variability": []}
    deviations_all: list[dict[str, Any]] = []
    measurement_ids: list[int] = []
    any_partial = False
    any_failed = False

    if dynamic_pricing_execution is not None:
        requested_all["dynamic_pricing"] = {
            "tool_call_id": dynamic_pricing_execution["tool_call_id"],
            "energy_scope": dynamic_pricing_execution["energy_scope"],
            "load_shift_entries": deepcopy(dynamic_pricing_execution["requested_entries"]),
            "demand_adjustments": deepcopy(dynamic_pricing_execution["requested_adjustments"]),
            "total_shifted_mw": dynamic_pricing_execution["requested_total_shifted_mw"],
        }
        actual_all["dynamic_pricing"] = {
            "tool_call_id": dynamic_pricing_execution["tool_call_id"],
            "energy_scope": dynamic_pricing_execution["energy_scope"],
            "response_factor": dynamic_pricing_execution["response_factor"],
            "load_shift_entries": deepcopy(dynamic_pricing_execution["actual_entries"]),
            "demand_adjustments": deepcopy(dynamic_pricing_execution["actual_adjustments"]),
            "total_shifted_mw": dynamic_pricing_execution["actual_total_shifted_mw"],
            "conservation_verified": dynamic_pricing_execution["conservation_verified"],
        }
        if abs(dynamic_pricing_execution["response_factor"] - 1.0) > 1e-12:
            any_partial = True
            for entry in dynamic_pricing_execution["actual_entries"]:
                deviations_all.append({
                    "asset_type": "flexible_load_shift",
                    "source_interval_index": entry["source_interval_index"],
                    "target_interval_index": entry["target_interval_index"],
                    "requested_mw": entry["requested_shifted_mw"],
                    "actual_mw": entry["actual_shifted_mw"],
                    "reason": "Synthetic Dynamic Pricing flexible-load response differed from the requested shift.",
                })

    for interval in intervals:
        requested, _ = _requested_for_interval(interval)
        requested_all["intervals"].append(deepcopy(interval))
        interval_index = interval.get("index")
        start_text = interval.get("start")
        if start_text:
            try:
                interval_time = datetime.fromisoformat(start_text)
            except ValueError:
                interval_time = _clock.current_time
            _clock.set(interval_time)
        interval_minutes = float(actions.get("interval_minutes", config.get("interval_minutes", 15)))

        requested_curtailment = _number(interval.get("solar_curtailment_mw"))
        actual_curtailment = 0.0 if requested_curtailment is None else max(0.0, float(requested_curtailment))
        actual_interval = {
            "index": interval_index,
            "start": _clock.current_time.isoformat(),
            "generators": [],
            "batteries": [],
            "solar_curtailment_mw": actual_curtailment,
        }
        interval_deviations = []

        for gid, point in (interval.get("generators") or {}).items():
            target = _number(point.get("output_mw") if isinstance(point, dict) else None)
            generator = next((g for g in generators if str(g.id) == str(gid)), None)
            if generator is None or target is None:
                actual = None
                reason = "Generator or requested target is unknown."
                any_failed = True
            elif generator.availability_status != "AVAILABLE":
                # An unavailable/tripped generator is expected to remain at 0 MW.
                # A plan that explicitly commands 0 MW is therefore satisfied, not
                # a partial execution.  Previously this branch returned actual=None
                # for every unavailable unit, which made command tracking differ and
                # forced every recoverable-problem execution into PARTIAL_FAIL.
                if abs(float(target)) <= 1e-9:
                    actual = 0.0
                    generator.current_output_mw = 0.0
                    reason = None
                else:
                    actual = 0.0
                    reason = (
                        f"Generator availability is {generator.availability_status}; "
                        "non-zero dispatch cannot be applied."
                    )
                    any_partial = True
                    interval_deviations.append({
                        "asset_type": "generator", "id": gid,
                        "requested_mw": target, "actual_mw": actual,
                        "reason": reason,
                    })
            else:
                current = _number(generator.current_output_mw)
                current = target if current is None else current
                ramp_limit = max(0.0, float(generator.ramp_rate_mw_per_min or 0.0) * interval_minutes)
                delta = target - current
                actual = current + max(-ramp_limit, min(ramp_limit, delta))
                if failure_enabled:
                    actual = current + (actual - current) * failure_factor
                if actual != target:
                    any_partial = True
                    reason = "Requested dispatch not fully reached within simulated execution interval."
                    interval_deviations.append({"asset_type": "generator", "id": gid,
                                                "requested_mw": target, "actual_mw": actual,
                                                "reason": reason})
                elif variability_enabled:
                    offset = _normal_execution_offset(
                        enabled=True,
                        max_abs_mw=generator_variability_mw,
                        target_mw=target,
                        apply_to_zero_targets=variability_zero_targets,
                    )
                    lower = max(0.0, float(generator.min_output_mw or 0.0), current - ramp_limit)
                    upper = min(float(generator.max_output_mw), current + ramp_limit)
                    varied = max(lower, min(upper, actual + offset))
                    if abs(varied - actual) > 1e-12:
                        actual_all["normal_variability"].append({
                            "interval": interval_index,
                            "asset_type": "generator",
                            "id": gid,
                            "requested_mw": target,
                            "actual_mw": varied,
                            "delta_mw": varied - target,
                            "reason": "Synthetic normal control/telemetry variability",
                        })
                        actual = varied
                generator.current_output_mw = actual
            actual_interval["generators"].append({"id": gid, "actual_mw": actual})
            if actual is None:
                interval_deviations.append({"asset_type": "generator", "id": gid,
                                            "requested_mw": target, "actual_mw": None,
                                            "reason": reason})

        for bid, point in (interval.get("batteries") or {}).items():
            target = _number(point.get("power_mw") if isinstance(point, dict) else None)
            battery = next((b for b in batteries if str(b.id) == str(bid)), None)
            if battery is None or target is None:
                actual = None
                reason = "Battery or requested target is unknown."
                any_failed = True
            elif battery.availability_status != "AVAILABLE":
                # Same semantics as an unavailable generator: an explicit 0 MW
                # command is already satisfied by keeping the unavailable battery
                # idle. Only a requested non-zero action is a partial execution.
                if abs(float(target)) <= 1e-9:
                    actual = 0.0
                    battery.current_power_mw = 0.0
                    reason = None
                else:
                    actual = 0.0
                    reason = (
                        f"Battery availability is {battery.availability_status}; "
                        "non-zero dispatch cannot be applied."
                    )
                    any_partial = True
                    interval_deviations.append({
                        "asset_type": "battery", "id": bid,
                        "requested_mw": target, "actual_mw": actual,
                        "reason": reason,
                    })
            else:
                bounded = max(-float(battery.max_charge_mw), min(float(battery.max_discharge_mw), target))
                actual = bounded
                if failure_enabled:
                    actual *= failure_factor
                if actual != target:
                    any_partial = True
                    reason = "Requested battery power was limited during simulated execution."
                    interval_deviations.append({"asset_type": "battery", "id": bid,
                                                "requested_mw": target, "actual_mw": actual,
                                                "reason": reason})
                elif variability_enabled:
                    offset = _normal_execution_offset(
                        enabled=True,
                        max_abs_mw=battery_variability_mw,
                        target_mw=target,
                        apply_to_zero_targets=variability_zero_targets,
                    )
                    varied = max(-float(battery.max_charge_mw), min(float(battery.max_discharge_mw), actual + offset))
                    if abs(varied - actual) > 1e-12:
                        actual_all["normal_variability"].append({
                            "interval": interval_index,
                            "asset_type": "battery",
                            "id": bid,
                            "requested_mw": target,
                            "actual_mw": varied,
                            "delta_mw": varied - target,
                            "reason": "Synthetic normal control/telemetry variability",
                        })
                        actual = varied
                battery.current_power_mw = actual
                if battery.capacity_mwh and battery.soc_pct is not None:
                    eff = battery.efficiency
                    if eff is not None and eff > 0:
                        dt_h = interval_minutes / 60.0
                        energy_delta = (actual * dt_h / eff) if actual >= 0 else (actual * dt_h * eff)
                        battery.soc_pct = max(0.0, min(100.0, battery.soc_pct - (energy_delta / battery.capacity_mwh) * 100.0))
            actual_interval["batteries"].append({"id": bid, "actual_mw": actual})
            if actual is None:
                interval_deviations.append({"asset_type": "battery", "id": bid,
                                            "requested_mw": target, "actual_mw": None,
                                            "reason": reason})

        # Optional solar forecast-error stress is applied to the executed solar
        # plant outputs themselves.  With the default 0% deviation this is a no-op.
        if solar_dev is not None and abs(float(solar_dev)) > 1e-12:
            factor = max(0.0, 1.0 + float(solar_dev) / 100.0)
            for item in actual_interval["generators"]:
                if item.get("actual_mw") is None:
                    continue
                generator = next((g for g in generators if str(g.id) == str(item.get("id"))), None)
                if generator is None or generator.availability_status != "AVAILABLE":
                    continue
                before_weather = float(item["actual_mw"])
                weather_actual = before_weather * factor
                if generator.max_output_mw is not None:
                    weather_actual = min(float(generator.max_output_mw), weather_actual)
                weather_actual = max(0.0, weather_actual)
                if abs(weather_actual - before_weather) > 1e-9:
                    any_partial = True
                    interval_deviations.append({
                        "asset_type": "generator", "id": item.get("id"),
                        "requested_mw": before_weather, "actual_mw": weather_actual,
                        "reason": "Solar availability deviation changed executed generation."
                    })
                    item["actual_mw"] = weather_actual
                    generator.current_output_mw = weather_actual

        _clock.advance(interval_minutes)

        # The generator rows are the solar fleet, so the authoritative solar
        # measurement is the sum of executed plant outputs.  Forecast solar is
        # context for planning; it is not extra generation added to dispatch.
        solar_actual_values = [
            _number(item.get("actual_mw"))
            for item in actual_interval.get("generators", [])
            if isinstance(item, dict) and item.get("actual_mw") is not None
        ]
        solar_actual = sum(solar_actual_values) if solar_actual_values else 0.0
        forecast_demand = _latest_forecast("demand", _clock.current_time, plan.snapshot_id)
        base_demand_actual = _actual_value(
            _number(forecast_demand.forecast_value) if forecast_demand else None,
            base_snapshot.demand_mw, demand_dev
        )
        demand_actual = base_demand_actual
        if demand_actual is not None and dynamic_pricing_execution is not None:
            demand_delta = dynamic_pricing_execution["actual_delta_by_interval"].get(int(interval_index), 0.0)
            demand_actual = float(demand_actual) + float(demand_delta)
            if demand_actual < -1e-9:
                any_failed = True
                interval_deviations.append({
                    "asset_type": "flexible_load_shift",
                    "interval": interval_index,
                    "requested_mw": demand_delta,
                    "actual_mw": None,
                    "reason": "Dynamic Pricing load shift would make simulated demand negative.",
                })
                demand_actual = 0.0
            else:
                demand_actual = max(0.0, demand_actual)
            actual_interval["dynamic_pricing_demand_delta_mw"] = demand_delta
            actual_interval["demand_before_dynamic_pricing_mw"] = base_demand_actual
            actual_interval["demand_after_dynamic_pricing_mw"] = demand_actual

        for variable, value in (("solar", solar_actual), ("demand", demand_actual)):
            if value is not None:
                measurement = ActualMeasurement(variable_name=variable, actual_value=value,
                                                unit="MW", timestamp=_clock.current_time,
                                                source="SIMULATION", is_synthetic=True)
                db.session.add(measurement)
                db.session.flush()
                measurement_ids.append(measurement.id)

        actual_all["intervals"].append(actual_interval)
        deviations_all.extend(interval_deviations)

    post_state = _state_snapshot_payload(generators, batteries)
    solar_values = [m.actual_value for m in ActualMeasurement.query.filter_by(source="SIMULATION", is_synthetic=True).filter(ActualMeasurement.timestamp == _clock.current_time, ActualMeasurement.variable_name == "solar").all()]
    demand_values = [m.actual_value for m in ActualMeasurement.query.filter_by(source="SIMULATION", is_synthetic=True).filter(ActualMeasurement.timestamp == _clock.current_time, ActualMeasurement.variable_name == "demand").all()]

    # Authoritative post-execution generation is the actual solar fleet output.
    # Do not add the forecast/measurement to generator dispatch: they represent
    # the same solar fleet and doing so was the source of the repeated +MW surplus
    # and NOT_ACHIEVED results after otherwise matched executions.
    actual_solar_generation_mw = _number(solar_values[-1] if solar_values else None)
    post_demand_mw = _number(demand_values[-1] if demand_values else base_snapshot.demand_mw)
    final_actual_interval = actual_all["intervals"][-1] if actual_all["intervals"] else {}
    generator_dispatch_mw = sum(
        float(item.get("actual_mw"))
        for item in (final_actual_interval.get("generators") or [])
        if isinstance(item, dict) and item.get("actual_mw") is not None
    )
    if actual_solar_generation_mw is None:
        actual_solar_generation_mw = generator_dispatch_mw
    solar_curtailment_mw = _number(final_actual_interval.get("solar_curtailment_mw")) or 0.0
    final_forecast_solar = _latest_forecast("solar", _clock.current_time, plan.snapshot_id)
    forecast_solar_reference_mw = (
        _number(final_forecast_solar.forecast_value) if final_forecast_solar else None
    )

    post_state["execution_balance_components"] = {
        "forecast_solar_reference_mw": forecast_solar_reference_mw,
        "actual_solar_generation_mw": actual_solar_generation_mw,
        "generator_dispatch_sum_mw": generator_dispatch_mw,
        "solar_curtailment_mw": solar_curtailment_mw,
        "demand_mw": post_demand_mw,
        "balance_semantics": "solar_generation + battery_power - demand",
    }
    post_snapshot = _create_snapshot(
        run_id=plan.run_id, timestamp=_clock.current_time, source_snapshot=base_snapshot,
        generators=generators, batteries=batteries,
        demand_mw=post_demand_mw,
        solar_gen_mw=actual_solar_generation_mw,
        other_gen_mw=base_snapshot.other_gen_mw,
        state_json=post_state, data_quality="POST_EXECUTION"
    )

    # Keep the simulated world usable for the next planning cycle.  The newly
    # created post-execution snapshot needs its own complete forecast window;
    # otherwise Tool 05 correctly rejects a second Generate Plans request at T+0.
    followup_cfg = config.get("followup_forecast", {})
    followup_forecast_ids: list[int] = []
    if bool(followup_cfg.get("enabled", True)):
        followup_interval_minutes = float(actions.get("interval_minutes", config.get("interval_minutes", 15)))
        followup_horizon_minutes = followup_interval_minutes * len(intervals)
        followup_forecast_ids = _create_followup_forecasts(
            post_snapshot=post_snapshot,
            interval_minutes=followup_interval_minutes,
            horizon_minutes=followup_horizon_minutes,
            solar_baseline_mw=actual_solar_generation_mw,
            demand_baseline_mw=post_demand_mw,
        )

    db.session.commit()
    if any_failed and any_partial:
        status = "PARTIAL_FAIL"
    elif any_failed:
        status = "FAILED"
    elif any_partial:
        status = "PARTIAL_FAIL"
    else:
        status = "SUCCESS"

    return {
        "execution_status": status,
        "requested_actions": requested_all,
        "actual_actions": actual_all,
        "deviations": deviations_all,
        "failure_information": ("One or more requested actions failed or were only partially achieved."
                                 if deviations_all else None),
        "pre_execution_snapshot_id": pre_snapshot.id,
        "post_execution_snapshot_id": post_snapshot.id,
        "actual_measurement_ids": measurement_ids,
        "followup_forecast_ids": followup_forecast_ids,
        "followup_forecast_source": "SIMULATION_PERSISTENCE" if followup_forecast_ids else None,
        "simulation_time": _clock.current_time.isoformat(),
        "simulation_notice": SIMULATION_STUB_NOTICE,
        "dynamic_pricing_execution": (
            {
                "tool_call_id": dynamic_pricing_execution["tool_call_id"],
                "requested_total_shifted_mw": dynamic_pricing_execution["requested_total_shifted_mw"],
                "actual_total_shifted_mw": dynamic_pricing_execution["actual_total_shifted_mw"],
                "response_factor": dynamic_pricing_execution["response_factor"],
                "conservation_verified": dynamic_pricing_execution["conservation_verified"],
            }
            if dynamic_pricing_execution is not None else None
        ),
    }


def monitoring_tick(*, active_plan_id: int | None = None, current_snapshot_id: int | None = None,
                    trigger_source: str = "SCHEDULED", threshold_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Advance monitoring through Tool 11 without embedding planning logic."""
    from database import AgentRunTrace, db
    from tools.operational_tools import monitor_system_conditions
    import uuid

    if active_plan_id is None or current_snapshot_id is None:
        return {"status": "REJECTED", "message": "active_plan_id and current_snapshot_id are required."}

    run = AgentRunTrace(run_uuid=str(uuid.uuid4()), start_time=_clock.current_time or datetime.now(timezone.utc).replace(tzinfo=None),
                        trigger_source=trigger_source, status="RUNNING")
    db.session.add(run)
    db.session.flush()
    result = monitor_system_conditions(active_plan_id, current_snapshot_id, threshold_profile=threshold_profile)
    run.status = "COMPLETED" if result.get("status") in ("SUCCESS", "PARTIAL") else "FAILED"
    run.end_time = _clock.current_time or datetime.now(timezone.utc).replace(tzinfo=None)
    run.final_outcome_summary = result.get("message")
    db.session.commit()
    return {"status": result.get("status"), "run_id": run.id, "monitoring": result}
