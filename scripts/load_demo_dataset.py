"""Reset the SolarGrid demo DB and load one synthetic dataset.

Usage from the project root:
    python scripts/load_demo_dataset.py normal
    python scripts/load_demo_dataset.py problem
    python scripts/load_demo_dataset.py dynamic_pricing
    python scripts/load_demo_dataset.py automation_trigger

WARNING: this intentionally resets all demo database tables.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Dataset seeding is a short-lived maintenance process, not the HTTP server.
# Never spend startup time warming Tool 16 here; ``python app.py`` will warm it
# asynchronously when the actual service starts.
os.environ["SOLARGRID_RAG_PREWARM"] = "false"
os.environ["SOLARGRID_AUTOMATION_ENABLED"] = "false"

from app import create_app
from solar_projects import EXPECTED_PROJECT_KEYS, SOLAR_PROJECTS_BY_KEY
from database import (
    ActualMeasurement,
    Battery,
    Forecast,
    Generator,
    GridBus,
    GridLine,
    ImbalanceEvent,
    Load,
    ReserveAssessment,
    SystemSnapshot,
    db,
)




DEFAULT_HORIZON_MINUTES = 120
FORECAST_INTERVAL_MINUTES = 15
REQUIRED_FORECAST_OFFSETS = tuple(range(0, DEFAULT_HORIZON_MINUTES + FORECAST_INTERVAL_MINUTES, FORECAST_INTERVAL_MINUTES))


def validate_dataset(data: dict) -> None:
    errors = []
    if float(data.get("snapshot", {}).get("other_gen_mw", -1)) != 0.0:
        errors.append("snapshot.other_gen_mw must be 0.0 for Solar-only operation")

    generators = data.get("generators") or []
    if not generators:
        errors.append("at least one solar generator is required")
    for idx, row in enumerate(generators):
        meta = row.get("constraints_json") or {}
        if str(meta.get("technology", "")).upper() != "SOLAR":
            errors.append(f"generators[{idx}].constraints_json.technology must be SOLAR")
        if str(meta.get("energy_source", "")).upper() != "SOLAR":
            errors.append(f"generators[{idx}].constraints_json.energy_source must be SOLAR")

    project_keys = []
    for idx, row in enumerate(generators):
        meta = row.get("constraints_json") or {}
        project_key = str(meta.get("project_key") or "")
        project_keys.append(project_key)
        reference = SOLAR_PROJECTS_BY_KEY.get(project_key)
        if reference is None:
            errors.append(f"generators[{idx}] has unknown project_key {project_key!r}")
            continue
        if row.get("name") != reference["name"]:
            errors.append(f"generators[{idx}].name must match the project identity for {project_key}")
        try:
            if abs(float(row.get("capacity_mw")) - float(reference["capacity_mw"])) > 1e-6:
                errors.append(f"generators[{idx}].capacity_mw must match reference nameplate capacity for {project_key}")
            if float(row.get("current_output_mw")) < 0 or float(row.get("max_output_mw")) < 0:
                errors.append(f"generators[{idx}] output/operating limits must be non-negative")
            if float(row.get("max_output_mw")) > float(row.get("capacity_mw")) + 1e-9:
                errors.append(f"generators[{idx}].max_output_mw cannot exceed capacity_mw")
        except (TypeError, ValueError):
            errors.append(f"generators[{idx}] has invalid numeric generation limits")

    if tuple(project_keys) != EXPECTED_PROJECT_KEYS:
        errors.append(
            "generator project order/coverage must match the six configured Jordan solar projects: "
            + ", ".join(EXPECTED_PROJECT_KEYS)
        )

    # Demo snapshots use one consistent solar-generation meaning: the aggregate
    # fleet output must equal the sum of plant current outputs.  This prevents a
    # forecast/telemetry value from being counted again on top of generator dispatch.
    try:
        snapshot_solar = float((data.get("snapshot") or {}).get("solar_gen_mw"))
        generator_solar = sum(float(row.get("current_output_mw")) for row in generators)
        if abs(snapshot_solar - generator_solar) > 1e-6:
            errors.append(
                f"snapshot.solar_gen_mw ({snapshot_solar}) must equal the sum of generator "
                f"current_output_mw ({generator_solar}) in demo datasets"
            )
    except (TypeError, ValueError):
        errors.append("snapshot.solar_gen_mw and generator current_output_mw must be numeric")

    contract = data.get("forecast_contract") or {}
    if contract.get("unit") != "MW":
        errors.append("forecast_contract.unit must be MW")
    if int(contract.get("interval_minutes", -1)) != FORECAST_INTERVAL_MINUTES:
        errors.append(f"forecast_contract.interval_minutes must be {FORECAST_INTERVAL_MINUTES}")
    if int(contract.get("horizon_minutes", -1)) != DEFAULT_HORIZON_MINUTES:
        errors.append(f"forecast_contract.horizon_minutes must be {DEFAULT_HORIZON_MINUTES}")
    variables = {str(v).lower() for v in (contract.get("variables") or [])}
    if variables != {"solar", "demand"}:
        errors.append("forecast_contract.variables must contain exactly solar and demand")

    forecasts = data.get("forecasts") or []
    by_offset = {}
    duplicate_offsets = []
    for idx, point in enumerate(forecasts):
        try:
            off = int(point["target_offset_minutes"])
            solar = float(point["solar_mw"])
            demand = float(point["demand_mw"])
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"forecasts[{idx}] is malformed: {exc}")
            continue
        if off in by_offset:
            duplicate_offsets.append(off)
        if solar < 0 or demand < 0:
            errors.append(f"forecasts[{idx}] solar_mw and demand_mw must be non-negative")
        by_offset[off] = {"solar_mw": solar, "demand_mw": demand}

    if duplicate_offsets:
        errors.append("duplicate forecast offsets (minutes): " + ", ".join(map(str, sorted(set(duplicate_offsets)))))

    missing = [off for off in REQUIRED_FORECAST_OFFSETS if off not in by_offset]
    extra = [off for off in sorted(by_offset) if off not in REQUIRED_FORECAST_OFFSETS]
    if missing:
        errors.append("missing forecast offsets (minutes): " + ", ".join(map(str, missing)))
    if extra:
        errors.append("unexpected forecast offsets outside the 120-minute contract: " + ", ".join(map(str, extra)))

    if errors:
        raise ValueError("Invalid SolarGrid demo dataset:\n- " + "\n- ".join(errors))

def load_json(name: str) -> dict:
    path = ROOT / "datasets" / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"Dataset not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def snapshot_state() -> dict:
    generators = Generator.query.order_by(Generator.id).all()
    batteries = Battery.query.order_by(Battery.id).all()
    loads = Load.query.order_by(Load.id).all()
    lines = GridLine.query.order_by(GridLine.id).all()
    return {
        "generators": [
            {
                "id": g.id,
                "name": g.name,
                "bus_id": g.bus_id,
                "capacity_mw": g.capacity_mw,
                "current_output_mw": g.current_output_mw,
                "min_output_mw": g.min_output_mw,
                "max_output_mw": g.max_output_mw,
                "ramp_rate_mw_per_min": g.ramp_rate_mw_per_min,
                "availability_status": g.availability_status,
                "constraints_json": g.constraints_json,
            }
            for g in generators
        ],
        "batteries": [
            {
                "id": b.id,
                "name": b.name,
                "bus_id": b.bus_id,
                "capacity_mwh": b.capacity_mwh,
                "soc_pct": b.soc_pct,
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
        "loads": [
            {
                "id": x.id,
                "name": x.name,
                "bus_id": x.bus_id,
                "p_mw": x.p_mw,
                "q_mvar": x.q_mvar,
                "load_type": x.load_type,
                "is_flexible": x.is_flexible,
            }
            for x in loads
        ],
        "grid_lines": [
            {
                "id": x.id,
                "line_name": x.line_name,
                "from_bus_id": x.from_bus_id,
                "to_bus_id": x.to_bus_id,
                "thermal_limit_mva": x.thermal_limit_mva,
                "loading_pct": x.loading_pct,
                "is_congested": x.is_congested,
            }
            for x in lines
        ],
        "weather": None,
    }


def seed(name: str) -> None:
    data = load_json(name)
    validate_dataset(data)
    app = create_app()

    with app.app_context():
        db.drop_all()
        db.create_all()

        for row in data["buses"]:
            db.session.add(GridBus(**row))
        db.session.flush()

        for row in data["grid_lines"]:
            db.session.add(GridLine(**row))
        for row in data["generators"]:
            db.session.add(Generator(**row))
        for row in data["batteries"]:
            db.session.add(Battery(**row))
        for row in data["loads"]:
            db.session.add(Load(**row))
        db.session.flush()

        now = datetime.utcnow()
        for row in data.get("actual_measurements", []):
            payload = dict(row)
            payload["timestamp"] = now
            db.session.add(ActualMeasurement(**payload))

        s = data["snapshot"]
        snapshot = SystemSnapshot(
            timestamp=now,
            demand_mw=s["demand_mw"],
            solar_gen_mw=s["solar_gen_mw"],
            other_gen_mw=s["other_gen_mw"],
            battery_soc_pct=s["battery_soc_pct"],
            reserve_margin_mw=s["reserve_margin_mw"],
            state_json=snapshot_state(),
            grid_status=s["grid_status"],
            data_quality=s["data_quality"],
            data_source=s["data_source"],
            is_synthetic=s.get("is_synthetic", True),
        )
        db.session.add(snapshot)
        db.session.flush()

        for point in data.get("forecasts", []):
            target = now + timedelta(minutes=int(point["target_offset_minutes"]))
            for variable, key in (("solar", "solar_mw"), ("demand", "demand_mw")):
                db.session.add(
                    Forecast(
                        snapshot_id=snapshot.id,
                        variable_name=variable,
                        forecast_value=float(point[key]),
                        unit="MW",
                        source="DEMO_DATASET",
                        issued_at=now,
                        target_time=target,
                        confidence_interval=None,
                    )
                )

        r = data["reserve_assessment"]
        db.session.add(
            ReserveAssessment(
                snapshot_id=snapshot.id,
                required_reserve_mw=r["required_reserve_mw"],
                actual_reserve_mw=r["actual_reserve_mw"],
                gen_contribution_mw=r["gen_contribution_mw"],
                battery_contribution_mw=r["battery_contribution_mw"],
                limiting_constraints=r.get("limiting_constraints", []),
                time_horizon_minutes=r["time_horizon_minutes"],
                status=r["status"],
            )
        )

        event = data.get("imbalance_event")
        if event:
            db.session.add(
                ImbalanceEvent(
                    snapshot_id=snapshot.id,
                    event_type=event["event_type"],
                    detection_time=now,
                    target_time=now + timedelta(minutes=int(event["target_offset_minutes"])),
                    severity=event["severity"],
                    expected_imbalance_mw=event["expected_imbalance_mw"],
                    risk_drivers=event.get("risk_drivers", []),
                    status=event.get("status", "OPEN"),
                )
            )

        db.session.commit()

        exp = data["expected_behavior"]
        print(f"Loaded dataset: {data['dataset_name']}")
        print(f"Snapshot ID: {snapshot.id}")
        print(f"Grid status: {s['grid_status']}")
        print(f"Demand: {s['demand_mw']:.2f} MW")
        print(f"Solar: {s['solar_gen_mw']:.2f} MW")
        print(f"Non-solar generation: {s['other_gen_mw']:.2f} MW (expected 0.00 in solar-only datasets)")
        print(f"Expected current imbalance: {exp['current_imbalance_mw']:.2f} MW ({exp['imbalance_direction']})")
        print(f"Reserve status: {r['status']} | margin {r['reserve_margin_mw']:.2f} MW")
        print(f"Expected future risk: {exp['future_risk_max_severity']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
    "scenario",
    choices=["normal", "problem", "dynamic_pricing", "automation_trigger"]
)
    args = parser.parse_args()
    seed(args.scenario)


if __name__ == "__main__":
    main()
