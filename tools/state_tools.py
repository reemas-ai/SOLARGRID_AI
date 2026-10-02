"""Corrected Tools 01-04, based only on the supplied state_tools.py implementation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import copy
import requests
from sqlalchemy.exc import SQLAlchemyError

from database import SystemSnapshot, Forecast, ActualMeasurement, Generator, Battery, Load, GridLine, ReserveAssessment, ImbalanceEvent, AgentRunTrace, ToolCallLog, ExternalDataProvenance
from runtime import tool_call, finish_log
from schemas import ToolStatus
import inspect

DEFAULT_HORIZON_MINUTES = 120
FORECAST_INTERVAL_MINUTES = 15

def _load_thresholds():
    from config_loader import load_json
    return load_json("thresholds.json")

TOOL_STATUS = {"SUCCESS", "PARTIAL", "REJECTED", "FAILED", "TIMEOUT"}
DOMAIN_STATUS = {"OK", "UNKNOWN", "NOT_CONFIGURED", "NOT_EVALUATED", "FEASIBLE", "INFEASIBLE"}


def _result(tool_status, domain_status, data=None, message=None, error=None):
    out = {"tool_status": tool_status, "domain_status": domain_status, "data": data}
    if message is not None:
        out["message"] = message
    if error is not None:
        out["error"] = error
    return out


def _utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)




def _planning_forecast_coverage(session, snapshot, horizon_minutes):
    """Return whether a snapshot has the complete solar+demand MW forecast contract.

    Execution creates technical snapshots (for example PRE_EXECUTION) that may not
    own planning forecasts.  Those snapshots must not become the source for the next
    operator assessment, otherwise Tool 02 cannot calculate required reserve and the
    UI correctly falls back to UNKNOWN.
    """
    if snapshot is None or snapshot.timestamp is None:
        return False

    expected = set(range(0, horizon_minutes + FORECAST_INTERVAL_MINUTES, FORECAST_INTERVAL_MINUTES))
    found = {"solar": set(), "demand": set()}
    start = _utc(snapshot.timestamp)
    rows = (
        session.query(Forecast)
        .filter(
            Forecast.snapshot_id == snapshot.id,
            Forecast.variable_name.in_(("solar", "demand")),
            Forecast.unit == "MW",
        )
        .order_by(Forecast.target_time.asc(), Forecast.id.asc())
        .all()
    )
    for row in rows:
        if row.target_time is None or row.forecast_value is None:
            continue
        variable = str(row.variable_name or "").lower()
        if variable not in found:
            continue
        minutes = (_utc(row.target_time) - start).total_seconds() / 60.0
        offset = int(round(minutes))
        # Forecast rows in this demo are aligned to the 15-minute contract.  The
        # small tolerance only protects against timestamp serialization noise.
        if abs(minutes - offset) <= 0.05 and offset in expected:
            found[variable].add(offset)

    return all(expected.issubset(found[variable]) for variable in ("solar", "demand"))


def _latest_planning_forecast_source(session, target, horizon_minutes):
    """Find the newest past snapshot that can actually support Tools 02 and 04.

    Do not simply choose the newest SystemSnapshot: execution-only snapshots may be
    newer but intentionally have no planning forecast rows.  Scanning recent past
    snapshots keeps the assessment deterministic and preserves the last complete
    forecast contract loaded/generated for the demo.
    """
    candidates = (
        session.query(SystemSnapshot)
        .filter(SystemSnapshot.timestamp <= target)
        .order_by(SystemSnapshot.timestamp.desc(), SystemSnapshot.id.desc())
        .limit(100)
        .all()
    )
    for candidate in candidates:
        if _planning_forecast_coverage(session, candidate, horizon_minutes):
            return candidate
    return None


def _copy_planning_forecasts(session, source_snapshot, destination_snapshot, horizon_minutes):
    """Associate complete approved MW forecasts with a newly assessed snapshot.

    Forecast values are not interpolated or invented here. Existing solar/demand
    MW points are copied by relative offset from the previous authoritative
    snapshot onto the new snapshot timeline.
    """
    expected_offsets = list(range(0, horizon_minutes + FORECAST_INTERVAL_MINUTES, FORECAST_INTERVAL_MINUTES))
    if source_snapshot is None:
        missing = [f"{variable}@T+{offset}" for offset in expected_offsets for variable in ("solar", "demand")]
        return 0, missing
    source_start = _utc(source_snapshot.timestamp)
    destination_start = _utc(destination_snapshot.timestamp)
    rows = (
        session.query(Forecast)
        .filter(
            Forecast.snapshot_id == source_snapshot.id,
            Forecast.variable_name.in_(("solar", "demand")),
            Forecast.unit == "MW",
        )
        .order_by(Forecast.target_time.asc())
        .all()
    )
    by_offset = {}
    for row in rows:
        if row.target_time is None or row.forecast_value is None:
            continue
        offset = int(round((_utc(row.target_time) - source_start).total_seconds() / 60.0))
        by_offset.setdefault(offset, {})[row.variable_name] = row

    missing = []
    copied = 0
    for offset in expected_offsets:
        pair = by_offset.get(offset, {})
        for variable in ("solar", "demand"):
            source = pair.get(variable)
            if source is None:
                missing.append(f"{variable}@T+{offset}")
                continue
            session.add(Forecast(
                snapshot_id=destination_snapshot.id,
                variable_name=variable,
                forecast_value=float(source.forecast_value),
                unit="MW",
                source=f"{source.source or 'FORECAST'}:ROLLED_FORWARD",
                issued_at=datetime.now(timezone.utc),
                target_time=destination_start + timedelta(minutes=offset),
                confidence_interval=copy.deepcopy(source.confidence_interval),
            ))
            copied += 1
    return copied, missing


def _policy_if_approved(policy_loader=None):
    try:
        if policy_loader is None:
            from config_loader import load_decision_policy
            policy_loader = load_decision_policy
        policy = policy_loader()
    except Exception:
        return None, "Decision Policy is unavailable"
    if str(policy.get("status", "")).upper() not in {"APPROVED", "ACTIVE", "CONFIGURED"}:
        return None, "Decision Policy is not approved/configured"
    return policy, None


def fetch_current_weather_by_city(city):
    try:
        geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                           params={"name": city, "count": 1, "language": "en", "format": "json"}, timeout=10)
        if geo.status_code != 200:
            return {"status": "FAILED", "data": None, "message": "Failed to fetch geocoding data"}
        results = geo.json().get("results") or []
        if not results:
            return {"status": "FAILED", "data": None, "message": "City not found"}
        latitude, longitude = results[0]["latitude"], results[0]["longitude"]
        weather = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": latitude, "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,direct_normal_irradiance"
        }, timeout=10)
        if weather.status_code != 200:
            return {"status": "FAILED", "data": None, "message": "Failed to fetch weather data"}
        current = weather.json().get("current", {})
        return {"status": "SUCCESS", "data": {
            "city": city, "latitude": latitude, "longitude": longitude,
            "temperature_2m": current.get("temperature_2m"),
            "relative_humidity_2m": current.get("relative_humidity_2m"),
            "wind_speed_10m": current.get("wind_speed_10m"),
            "direct_normal_irradiance": current.get("direct_normal_irradiance"),
        }}
    except Exception as exc:
        return {"status": "FAILED", "data": None, "message": str(exc)}


def _snapshot_state(generators, batteries, loads, grid_lines, weather):
    return {
        "generators": [{
            "id": g.id, "name": g.name, "bus_id": g.bus_id, "capacity_mw": g.capacity_mw,
            "current_output_mw": g.current_output_mw, "min_output_mw": g.min_output_mw,
            "max_output_mw": g.max_output_mw, "ramp_rate_mw_per_min": g.ramp_rate_mw_per_min,
            "availability_status": g.availability_status, "constraints_json": copy.deepcopy(g.constraints_json)
        } for g in generators],
        "batteries": [{
            "id": b.id, "name": b.name, "bus_id": b.bus_id, "capacity_mwh": b.capacity_mwh,
            "soc_pct": b.soc_pct, "min_soc_pct": b.min_soc_pct, "max_soc_pct": b.max_soc_pct,
            "max_charge_mw": b.max_charge_mw, "max_discharge_mw": b.max_discharge_mw,
            "efficiency": b.efficiency, "current_power_mw": b.current_power_mw,
            "availability_status": b.availability_status
        } for b in batteries],
        "loads": [{"id": l.id, "name": l.name, "bus_id": l.bus_id, "p_mw": l.p_mw,
                    "q_mvar": l.q_mvar, "load_type": l.load_type, "is_flexible": l.is_flexible} for l in loads],
        "grid_lines": [{"id": x.id, "line_name": x.line_name, "from_bus_id": x.from_bus_id,
                        "to_bus_id": x.to_bus_id, "thermal_limit_mva": x.thermal_limit_mva,
                        "loading_pct": x.loading_pct, "is_congested": x.is_congested} for x in grid_lines],
        "weather": copy.deepcopy(weather),
    }


def _latest_current_solar_measurement(session, target):
    """Return the latest explicit current Solar MW measurement at or before target."""
    query_target = _utc(target).replace(tzinfo=None)
    return (
        session.query(ActualMeasurement)
        .filter(
            ActualMeasurement.variable_name == "solar",
            ActualMeasurement.unit == "MW",
            ActualMeasurement.timestamp <= query_target,
        )
        .order_by(ActualMeasurement.timestamp.desc(), ActualMeasurement.id.desc())
        .first()
    )


def _weather_from_external_record(session, record_id):
    """Load validated Open-Meteo context by provenance id without another network call."""
    if record_id is None:
        return None, None, None
    record = session.get(ExternalDataProvenance, int(record_id))
    if record is None:
        return None, "UNAVAILABLE", None
    status = str(record.status or "UNAVAILABLE").upper()
    if record.source != "OPEN_METEO" or status not in {"FRESH", "CACHED"}:
        return None, status, record
    payload = record.payload_json if isinstance(record.payload_json, dict) else {}
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    weather = {
        "provenance_id": record.id,
        "source": record.source,
        "source_role": record.source_role,
        "status": status,
        "retrieved_at": record.retrieved_at.isoformat() + "Z" if record.retrieved_at else None,
        "observed_at": record.observed_at.isoformat() + "Z" if record.observed_at else None,
        "location": record.location,
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "temperature_2m": current.get("temperature_2m"),
        "cloud_cover": current.get("cloud_cover"),
        "shortwave_radiation": current.get("shortwave_radiation"),
        "direct_normal_irradiance": current.get("direct_normal_irradiance"),
        "current_units": payload.get("current_units") or {},
    }
    return weather, status, record


def _store_external_irradiance_forecasts(session, snapshot_id, target, horizon_minutes, record):
    """Persist public solar-context forecasts in W/m2 only; never convert them to MW."""
    if record is None or record.source != "OPEN_METEO" or record.status not in {"FRESH", "CACHED"}:
        return 0
    payload = record.payload_json if isinstance(record.payload_json, dict) else {}
    hourly = payload.get("hourly") if isinstance(payload.get("hourly"), dict) else {}
    times = hourly.get("time") or []
    dni = hourly.get("direct_normal_irradiance") or []
    count = 0
    for t_str, irr in zip(times, dni):
        if irr is None or not t_str:
            continue
        try:
            t_dt = _utc(datetime.fromisoformat(str(t_str).replace("Z", "+00:00")))
        except (TypeError, ValueError):
            continue
        if target <= t_dt <= target + timedelta(minutes=horizon_minutes):
            session.add(Forecast(
                snapshot_id=snapshot_id, variable_name="solar_irradiance_dni",
                forecast_value=float(irr), unit="W/m2", source="OPEN_METEO",
                issued_at=record.retrieved_at or datetime.now(timezone.utc), target_time=t_dt,
            ))
            count += 1
    return count


def get_system_state(session, run_id=None, snapshot_time=None, forecast_horizon_minutes=DEFAULT_HORIZON_MINUTES,
                     city=None, latitude=None, longitude=None, external_weather_record_id=None):
    if forecast_horizon_minutes <= 0:
        return _result("REJECTED", "UNKNOWN", None, "horizon_minutes must be > 0")
    try:
        target = snapshot_time if snapshot_time is not None else datetime.now(timezone.utc)
        target = _utc(target)
        generators = session.query(Generator).all()
        batteries = session.query(Battery).all()
        loads = session.query(Load).all()
        grid_lines = session.query(GridLine).all()
        demand_mw = sum(l.p_mw for l in loads) if loads and all(l.p_mw is not None for l in loads) else None
        solar_measurement = _latest_current_solar_measurement(session, target)
        measured_solar_mw = (
            float(solar_measurement.actual_value)
            if solar_measurement is not None and solar_measurement.actual_value is not None
            else None
        )
        generator_values = [g.current_output_mw for g in generators]
        modeled_solar_mw = sum(generator_values) if generator_values and all(v is not None for v in generator_values) else (0.0 if not generator_values else None)
        # SolarGrid is configured as a solar-only generation system. Generator rows
        # represent controllable solar plants for planning/constraint purposes.
        # The current measurement is the authoritative total solar fleet output
        # when available; otherwise fall back to the modeled plant sum.
        solar_gen_mw = measured_solar_mw if measured_solar_mw is not None else modeled_solar_mw
        other_gen_mw = 0.0 if solar_gen_mw is not None else None
        soc_values = [b.soc_pct for b in batteries]
        battery_soc_pct = sum(soc_values) / len(soc_values) if soc_values and all(v is not None for v in soc_values) else None
        weather_state = None
        weather_status = None
        external_record = None
        if external_weather_record_id is not None:
            weather_state, weather_status, external_record = _weather_from_external_record(
                session, external_weather_record_id
            )
        elif city is not None:
            # Legacy compatibility path. New application flows persist provenance
            # through ExternalDataService and pass external_weather_record_id.
            wr = fetch_current_weather_by_city(city); weather_status = wr["status"]
            weather_state = wr.get("data")
        elif latitude is not None and longitude is not None:
            try:
                wr = requests.get("https://api.open-meteo.com/v1/forecast", params={
                    "latitude": latitude, "longitude": longitude,
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,direct_normal_irradiance"
                }, timeout=10)
                if wr.status_code == 200:
                    c = wr.json().get("current", {})
                    weather_state = {"latitude": latitude, "longitude": longitude,
                                    "temperature_2m": c.get("temperature_2m"),
                                    "relative_humidity_2m": c.get("relative_humidity_2m"),
                                    "wind_speed_10m": c.get("wind_speed_10m"),
                                    "direct_normal_irradiance": c.get("direct_normal_irradiance")}
                    weather_status = "SUCCESS"
                else:
                    weather_status = "FAILED"
            except Exception:
                weather_status = "FAILED"

        # Use the newest snapshot with a complete planning forecast contract.
        # A PRE_EXECUTION/technical snapshot can be newer than the last operator
        # assessment but have zero forecast rows; selecting it caused Reserve and
        # Future Risk to become UNKNOWN immediately after a successful execution.
        source_snapshot = _latest_planning_forecast_source(
            session, target, forecast_horizon_minutes
        )
        state_json = _snapshot_state(generators, batteries, loads, grid_lines, weather_state)
        has_required_state = demand_mw is not None and solar_gen_mw is not None and battery_soc_pct is not None
        grid_status = "WARNING" if any(x.is_congested for x in grid_lines) else "STABLE"
        weather_is_healthy = weather_status in (None, "SUCCESS", "FRESH")
        quality = "GOOD" if has_required_state and weather_is_healthy else "DEGRADED"
        snapshot = SystemSnapshot(run_id=run_id, timestamp=target, demand_mw=demand_mw,
                                  solar_gen_mw=solar_gen_mw, other_gen_mw=other_gen_mw,
                                  battery_soc_pct=battery_soc_pct, reserve_margin_mw=None,
                                  state_json=state_json, grid_status=grid_status,
                                  data_quality=quality,
                                  data_source="HYBRID:SIMULATION+OPEN_METEO" if weather_state else "SIMULATION",
                                  is_synthetic=True)
        session.add(snapshot); session.flush()

        copied_forecasts, missing_planning_forecasts = _copy_planning_forecasts(
            session, source_snapshot, snapshot, forecast_horizon_minutes
        )
        forecast_failure = bool(missing_planning_forecasts)

        external_irradiance_rows = 0
        if external_record is not None:
            external_irradiance_rows = _store_external_irradiance_forecasts(
                session, snapshot.id, target, forecast_horizon_minutes, external_record
            )
        elif weather_state and weather_state.get("latitude") is not None and weather_state.get("longitude") is not None:
            # Legacy direct-call path retained for backwards compatibility.
            try:
                resp = requests.get("https://api.open-meteo.com/v1/forecast", params={
                    "latitude": weather_state["latitude"], "longitude": weather_state["longitude"],
                    "hourly": "direct_normal_irradiance,temperature_2m", "forecast_days": 1
                }, timeout=10)
                if resp.status_code != 200:
                    forecast_failure = True
                else:
                    hourly = resp.json().get("hourly", {})
                    for t_str, irr in zip(hourly.get("time", []), hourly.get("direct_normal_irradiance", [])):
                        t_dt = _utc(datetime.fromisoformat(t_str))
                        if target <= t_dt <= target + timedelta(minutes=forecast_horizon_minutes):
                            session.add(Forecast(snapshot_id=snapshot.id, variable_name="solar_irradiance_dni",
                                                 forecast_value=float(irr) if irr is not None else None,
                                                 unit="W/m2", source="OPEN_METEO", issued_at=datetime.now(timezone.utc),
                                                 target_time=t_dt))
                            external_irradiance_rows += 1
            except Exception:
                forecast_failure = True

        if forecast_failure:
            snapshot.data_quality = "DEGRADED"
        session.commit()
        tool_status = "SUCCESS" if quality == "GOOD" and not forecast_failure else "PARTIAL"
        domain = "OK" if tool_status == "SUCCESS" else "UNKNOWN"
        return _result(tool_status, domain, {
            "snapshot_id": snapshot.id, "timestamp": target.isoformat(), "demand_mw": demand_mw,
            "solar_gen_mw": solar_gen_mw, "other_gen_mw": other_gen_mw, "battery_soc_pct": battery_soc_pct,
            "grid_status": grid_status, "data_quality": snapshot.data_quality,
            "data_source": snapshot.data_source, "is_synthetic": snapshot.is_synthetic,
            "external_data_provenance_id": external_record.id if external_record is not None else None,
            "external_data_status": weather_status,
            "external_data_retrieved_at": (external_record.retrieved_at.isoformat() + "Z") if external_record is not None and external_record.retrieved_at else None,
            "external_data_location": external_record.location if external_record is not None else None,
            "external_irradiance_forecasts_stored": external_irradiance_rows,
            "state_json": copy.deepcopy(state_json),
            "planning_forecast_source_snapshot_id": source_snapshot.id if source_snapshot is not None else None,
            "planning_forecasts_copied": copied_forecasts,
            "missing_planning_forecasts": missing_planning_forecasts
        }, "System state snapshot recorded successfully" if tool_status == "SUCCESS" else "Snapshot recorded with degraded/unknown evidence")
    except SQLAlchemyError as exc:
        session.rollback(); return _result("FAILED", "UNKNOWN", None, error=str(exc))
    except Exception as exc:
        session.rollback(); return _result("FAILED", "UNKNOWN", None, error=str(exc))


def _matching_policy(policy_loader=None):
    return _policy_if_approved(policy_loader)


def calculate_reserve(session, snapshot_id, horizon_minutes=DEFAULT_HORIZON_MINUTES, policy_loader=None):
    if horizon_minutes <= 0:
        return _result("REJECTED", "UNKNOWN", None, "horizon_minutes must be > 0")
    try:
        snapshot = session.query(SystemSnapshot).filter_by(id=snapshot_id).first()
        if not snapshot:
            return _result("REJECTED", "UNKNOWN", None, "System snapshot not found")
        state = snapshot.state_json or {}
        gens, bats = state.get("generators"), state.get("batteries")
        if gens is None or bats is None:
            return _result("PARTIAL", "UNKNOWN", None, "Historical generator/battery state is missing")
        limits = []
        gen_up = 0.0; bat_up = 0.0; unknown = False
        for g in gens:
            if str(g.get("availability_status")).upper() != "AVAILABLE":
                continue
            vals = (g.get("current_output_mw"), g.get("max_output_mw"), g.get("ramp_rate_mw_per_min"))
            if any(v is None for v in vals):
                unknown = True; limits.append({"asset_type":"GENERATOR","asset_id":g.get("id"),"constraint":"MISSING_DATA"}); continue
            cap_head = max(0.0, g["max_output_mw"] - g["current_output_mw"])
            ramp_head = max(0.0, g["ramp_rate_mw_per_min"] * horizon_minutes)
            gen_up += min(cap_head, ramp_head)
            if ramp_head < cap_head: limits.append({"asset_type":"GENERATOR","asset_id":g.get("id"),"constraint":"RAMP_LIMITED"})
        hours = horizon_minutes / 60.0
        for b in bats:
            if str(b.get("availability_status")).upper() != "AVAILABLE": continue
            vals=(b.get("soc_pct"),b.get("min_soc_pct"),b.get("capacity_mwh"),b.get("max_discharge_mw"),b.get("efficiency"),b.get("current_power_mw"))
            if any(v is None for v in vals) or b["capacity_mwh"] <= 0 or b["efficiency"] <= 0 or b["efficiency"] > 1:
                unknown=True; limits.append({"asset_type":"BATTERY","asset_id":b.get("id"),"constraint":"MISSING_OR_INVALID_DATA"}); continue
            energy = max(0.0, (b["soc_pct"]-b["min_soc_pct"])/100.0*b["capacity_mwh"]*b["efficiency"])
            power_head = max(0.0, b["max_discharge_mw"] - max(0.0, b["current_power_mw"]))
            bat_up += min(power_head, energy/hours)
        actual = gen_up + bat_up if not unknown else None
        policy, policy_issue = _matching_policy(policy_loader)
        required = None
        if policy is not None:
            if snapshot.demand_mw is None:
                required = None
            else:
                mw_rows = session.query(Forecast).filter(Forecast.snapshot_id==snapshot_id, Forecast.variable_name=="solar", Forecast.unit=="MW").order_by(Forecast.target_time.asc()).all()
                vals=[float(r.forecast_value) for r in mw_rows if r.forecast_value is not None]
                changes=[abs(b-a) for a,b in zip(vals,vals[1:])]
                if vals and len(vals)>=2:
                    required=float(policy["required_reserve"]["reserve_fraction"])*snapshot.demand_mw + float(policy["required_reserve"]["solar_variability_fraction"])*max(changes,default=0.0)
                elif not policy.get("required_reserve",{}).get("solar_variability_fraction",0.0):
                    required=float(policy["required_reserve"]["reserve_fraction"])*snapshot.demand_mw
                else:
                    required=None
        domain = "OK" if required is not None and actual is not None else "UNKNOWN"
        status = "SUFFICIENT" if domain == "OK" and actual >= required else ("INSUFFICIENT" if domain == "OK" else "UNKNOWN")
        assessment = ReserveAssessment(snapshot_id=snapshot_id, required_reserve_mw=required, actual_reserve_mw=actual,
                                       gen_contribution_mw=gen_up if not unknown else None,
                                       battery_contribution_mw=bat_up if not unknown else None,
                                       limiting_constraints=limits, time_horizon_minutes=float(horizon_minutes), status=status)
        session.add(assessment); session.flush()
        if actual is not None and required is not None: snapshot.reserve_margin_mw = actual-required
        session.commit()
        msg = "Reserve calculated" if domain == "OK" else "Reserve calculated with unknown required policy or physical evidence"
        if policy_issue: msg += f"; SPECIFICATION ISSUE: {policy_issue}"
        return _result("SUCCESS" if domain == "OK" else "PARTIAL", domain, {
            "assessment_id": assessment.id, "snapshot_id": snapshot_id, "required_reserve_mw": required,
            "actual_reserve_mw": actual, "gen_contribution_mw": assessment.gen_contribution_mw,
            "battery_contribution_mw": assessment.battery_contribution_mw, "reserve_margin_mw": snapshot.reserve_margin_mw,
            "limiting_constraints": limits, "reserve_status": status
        }, msg)
    except SQLAlchemyError as exc:
        session.rollback(); return _result("FAILED", "UNKNOWN", None, error=str(exc))
    except Exception as exc:
        session.rollback(); return _result("FAILED", "UNKNOWN", None, error=str(exc))


def detect_imbalance(session, snapshot_id, policy_loader=None):
    try:
        snapshot = session.query(SystemSnapshot).filter_by(id=snapshot_id).first()
        if not snapshot: return _result("REJECTED", "UNKNOWN", None, "System snapshot not found")
        state = snapshot.state_json or {}
        demand = snapshot.demand_mw; solar = snapshot.solar_gen_mw; other = snapshot.other_gen_mw
        bats = state.get("batteries")
        if demand is None or solar is None or other is None or bats is None or any(b.get("current_power_mw") is None for b in bats):
            return _result("PARTIAL", "UNKNOWN", {"snapshot_id": snapshot_id, "direction":"UNKNOWN", "imbalance_mw":None}, "Required current evidence is missing")
        battery_power = sum(b["current_power_mw"] for b in bats)
        residual = demand - solar - other - battery_power
        direction = "BALANCED" if residual == 0 else ("DEFICIT" if residual > 0 else "SURPLUS")
        policy, issue = _matching_policy(policy_loader)
        # Tool 02 can legitimately persist more than one reserve assessment for
        # the same snapshot (for example the seeded demo context plus the current
        # 120-minute assessment). The assessment invoked immediately before this
        # tool is the authoritative current context, so use the newest complete
        # record rather than treating multiple historical rows as ambiguous.
        reserve = (
            session.query(ReserveAssessment)
            .filter_by(snapshot_id=snapshot_id)
            .order_by(ReserveAssessment.id.desc())
            .first()
        )
        reserve_adequate = None
        if reserve is not None and reserve.actual_reserve_mw is not None and reserve.required_reserve_mw is not None:
            reserve_adequate = reserve.actual_reserve_mw + 1e-9 >= reserve.required_reserve_mw
        thresholds = _load_thresholds()
        tolerance = float(thresholds["imbalance"]["tolerance_mw"])
        magnitude = abs(float(residual))
        if reserve_adequate is None:
            severity = "UNKNOWN"
        elif magnitude <= tolerance:
            severity = "LOW"
        elif reserve_adequate:
            severity = "MEDIUM"
        else:
            severity = "HIGH"
        event_required = severity in {"HIGH", "CRITICAL"}
        drivers = ["demand", "solar_generation", "other_generation", "battery_power"]
        data = {"snapshot_id":snapshot_id,"direction":direction,"imbalance_mw":magnitude,"severity":severity,
                "reserve_adequate":reserve_adequate,"event_required":event_required,"drivers":drivers}
        domain = "OK" if severity != "UNKNOWN" else "UNKNOWN"
        status = "SUCCESS" if domain == "OK" else "PARTIAL"
        return _result(status, domain, data, "Current imbalance evaluated with the approved demo policy" + (f"; {issue}" if issue else ""))
    except SQLAlchemyError as exc:
        session.rollback(); return _result("FAILED", "UNKNOWN", None, error=str(exc))
    except Exception as exc:
        session.rollback(); return _result("FAILED", "UNKNOWN", None, error=str(exc))


def assess_future_risk(session, snapshot_id, horizon_minutes=120, policy_loader=None):
    if horizon_minutes <= 0: return _result("REJECTED", "UNKNOWN", None, "horizon_minutes must be > 0")
    try:
        snapshot=session.query(SystemSnapshot).filter_by(id=snapshot_id).first()
        if not snapshot: return _result("REJECTED", "UNKNOWN", None, "System snapshot not found")
        start=_utc(snapshot.timestamp); end=start+timedelta(minutes=horizon_minutes)
        forecasts=session.query(Forecast).filter(Forecast.snapshot_id==snapshot_id,
                                                  Forecast.target_time>=start, Forecast.target_time<=end).order_by(Forecast.target_time.asc()).all()
        # Only approved MW forecasts may feed risk. Weather irradiance is deliberately excluded.
        by_time={}
        for f in forecasts:
            if str(f.unit).upper() != "MW": continue
            if f.variable_name not in {"solar", "demand"}: continue
            by_time.setdefault(_utc(f.target_time), {})[f.variable_name]=f
        # Select reserve evidence from the same planning horizon requested by
        # this risk assessment. Multiple historical reserve rows are valid and
        # must not force the domain to UNKNOWN. Prefer the newest complete match.
        reserve_rows=(
            session.query(ReserveAssessment)
            .filter_by(snapshot_id=snapshot_id)
            .order_by(ReserveAssessment.id.desc())
            .all()
        )
        reserve=next((
            item for item in reserve_rows
            if item.time_horizon_minutes is not None
            and abs(float(item.time_horizon_minutes)-float(horizon_minutes)) <= 1e-9
            and item.actual_reserve_mw is not None
            and item.required_reserve_mw is not None
        ), None)
        policy, policy_issue=_matching_policy(policy_loader)
        if not by_time or reserve is None or reserve.actual_reserve_mw is None:
            return _result("PARTIAL", "UNKNOWN", {"snapshot_id":snapshot_id,"horizon_minutes":horizon_minutes,"timeline":[],"highest_risk_period":None,"max_severity":"UNKNOWN"},
                           "Future risk is UNKNOWN because approved MW forecasts and/or context-matched reserve evidence are missing")
        thresholds = _load_thresholds()
        freshness_limit = float(thresholds.get("forecast_freshness_minutes", 60.0))
        tolerance = float(thresholds["imbalance"]["tolerance_mw"])
        # ReserveAssessment is a context-level reserve bound for the requested planning horizon.
        reserve_actual = reserve.actual_reserve_mw
        reserve_required = reserve.required_reserve_mw
        reserve_margin = None if reserve_actual is None or reserve_required is None else reserve_actual - reserve_required
        other_gen = snapshot.other_gen_mw
        battery_power = sum((b.get("current_power_mw") or 0.0) for b in (snapshot.state_json or {}).get("batteries", []))
        timeline=[]
        for target_time, pair in sorted(by_time.items()):
            solar = pair.get("solar")
            demand = pair.get("demand")
            if solar is None or demand is None or solar.forecast_value is None or demand.forecast_value is None:
                continue
            ages=[]
            for f in (solar, demand):
                if f.issued_at is None:
                    ages.append(float("inf"))
                else:
                    ages.append(max(0.0, (_utc(snapshot.timestamp) - _utc(f.issued_at)).total_seconds()/60.0))
            if any(age > freshness_limit for age in ages):
                timeline.append({"target_time":target_time.isoformat(),"risk_level":"UNKNOWN","expected_imbalance_mw":None,
                                 "reserve_margin_mw":reserve_margin,"drivers":["STALE_FORECAST"],"affected_resources":[],"constraints":[],
                                 "data_quality":{"forecast_freshness_minutes":max(ages)}})
                continue
            expected = float(demand.forecast_value) - float(solar.forecast_value) - float(other_gen or 0.0) - float(battery_power)
            magnitude = abs(expected)
            if reserve_actual is None or reserve_required is None:
                level="UNKNOWN"
            elif magnitude > float(reserve_actual):
                level="CRITICAL"
            elif reserve_margin < 0:
                level="HIGH"
            elif magnitude > tolerance:
                level="MEDIUM"
            else:
                level="LOW"
            timeline.append({"target_time":target_time.isoformat(),"risk_level":level,"expected_imbalance_mw":magnitude,
                             "reserve_margin_mw":reserve_margin,"drivers":["demand_forecast","solar_forecast"],
                             "affected_resources":[],"constraints":[],"data_quality":{"forecast_freshness_minutes":max(ages)}})
        if not timeline:
            return _result("PARTIAL", "UNKNOWN", {"snapshot_id":snapshot_id,"horizon_minutes":horizon_minutes,"timeline":[],"highest_risk_period":None,"max_severity":"UNKNOWN"},
                           "No complete fresh MW solar+demand forecast pair was available")
        order={"LOW":0,"MEDIUM":1,"HIGH":2,"CRITICAL":3,"UNKNOWN":-1}
        highest=max(timeline,key=lambda x:(order.get(x["risk_level"],-1), x["target_time"]))
        max_severity=highest["risk_level"]
        if max_severity in {"HIGH","CRITICAL"}:
            target_dt=_utc(datetime.fromisoformat(highest["target_time"]))
            existing=session.query(ImbalanceEvent).filter_by(snapshot_id=snapshot_id,event_type="FUTURE_RISK",target_time=target_dt).first()
            if existing is None and highest.get("expected_imbalance_mw") is not None:
                existing=ImbalanceEvent(snapshot_id=snapshot_id,event_type="FUTURE_RISK",detection_time=datetime.now(timezone.utc),
                                        target_time=target_dt,severity=max_severity,expected_imbalance_mw=float(highest["expected_imbalance_mw"]),
                                        risk_drivers=highest.get("drivers",[]),status="OPEN")
                session.add(existing); session.flush()
            risk_event_id=existing.id if existing is not None else None
        else:
            risk_event_id=None
        domain="OK" if all(x["risk_level"] != "UNKNOWN" for x in timeline) else "UNKNOWN"
        return _result("SUCCESS" if domain=="OK" else "PARTIAL", domain,
                       {"snapshot_id":snapshot_id,"horizon_minutes":horizon_minutes,"timeline":timeline,
                        "highest_risk_period":highest["target_time"],"max_severity":max_severity,"risk_event_id":risk_event_id},
                       "Future risk assessed using fresh MW forecasts and approved demo thresholds")
    except SQLAlchemyError as exc:
        session.rollback(); return _result("FAILED", "UNKNOWN", None, error=str(exc))
    except Exception as exc:
        session.rollback(); return _result("FAILED", "UNKNOWN", None, error=str(exc))


# ---------------------------------------------------------------------------
# Shared-runtime boundary for Tools 01–04
# ---------------------------------------------------------------------------
# The physical/state logic above remains unchanged. These wrappers make the
# public tool boundary use the same audit runtime as Tools 05–09.
def _tool_status_from_result(result):
    value = str(result.get("tool_status", "FAILED")).upper() if isinstance(result, dict) else "FAILED"
    try:
        return ToolStatus(value)
    except Exception:
        return ToolStatus.FAILED

#by remas
def _runtime_wrap(function, tool_name, tool_category):
    def wrapped(session, *args, run_id=None, **kwargs):
        input_payload = {
            "args": args,
            "kwargs": kwargs,
        }
        with tool_call(
            SimpleNamespace(session=session),
            AgentRunTrace,
            ToolCallLog,
            tool_name=tool_name,
            tool_category=tool_category,
            input_payload=input_payload,
            run_id=run_id,
        ) as (_rid, log):
            #result = function(session, *args, **kwargs) edit
            if "run_id" in inspect.signature(function).parameters:
               result = function(session, *args, run_id=run_id, **kwargs)
            else:
               result = function(session, *args, **kwargs)
            #end edit
            finish_log(log, result if isinstance(result, dict) else {"result": result},
                       _tool_status_from_result(result))
            session.commit()
            return result
    wrapped.__name__ = function.__name__
    wrapped.__doc__ = function.__doc__
    return wrapped


_get_system_state_impl = get_system_state
_calculate_reserve_impl = calculate_reserve
_detect_imbalance_impl = detect_imbalance
_assess_future_risk_impl = assess_future_risk

get_system_state = _runtime_wrap(_get_system_state_impl, "get_system_state", "STATE")
calculate_reserve = _runtime_wrap(_calculate_reserve_impl, "calculate_reserve", "STATE")
detect_imbalance = _runtime_wrap(_detect_imbalance_impl, "detect_imbalance", "STATE")
assess_future_risk = _runtime_wrap(_assess_future_risk_impl, "assess_future_risk", "STATE")
