"""Deterministic regression for the SolarGrid planning/execution balance contract.

This script intentionally avoids Flask/database imports.  It validates the LP
boundary used by Tool 05 against the two shipped demo datasets and checks that a
feasible final dispatch balances demand using the same semantics as Tool 14:

    solar generator output + battery power - demand = residual

Run from the project root:
    python scripts/verify_planning_execution_consistency.py
"""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemas import Strategy  # noqa: E402
from tools.planning_tools import _build_candidate  # noqa: E402


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def objects_for(data: dict):
    generators = [SimpleNamespace(id=row["id"], name=row["name"]) for row in data["generators"]]
    batteries = [SimpleNamespace(id=row["id"], name=row["name"]) for row in data["batteries"]]
    return generators, batteries


def snapshot_for(data: dict, state: dict | None = None, *, solar_mw: float | None = None):
    state = state or {
        "generators": copy.deepcopy(data["generators"]),
        "batteries": copy.deepcopy(data["batteries"]),
    }
    return SimpleNamespace(
        state_json=state,
        solar_gen_mw=float(data["snapshot"]["solar_gen_mw"] if solar_mw is None else solar_mw),
        timestamp=datetime(2026, 1, 1),
    )


def planning_series(data: dict) -> list[dict]:
    # Tool 05 dispatches eight 15-minute intervals against the END boundaries
    # T+15 ... T+120.
    return [
        {"solar": float(point["solar_mw"]), "demand": float(point["demand_mw"])}
        for point in data["forecasts"][1:]
    ]


def final_balance(candidate: dict, demand: float) -> float:
    interval = candidate["dispatch"].intervals[-1]
    generation = sum(point.output_mw for point in interval.generators.values())
    battery = sum(point.power_mw for point in interval.batteries.values())
    return float(generation + battery - demand)


def apply_dispatch_to_state(state: dict, dispatch) -> None:
    dt_h = dispatch.interval_minutes / 60.0
    for interval in dispatch.intervals:
        for gid, point in interval.generators.items():
            for row in state["generators"]:
                if str(row["id"]) == str(gid):
                    row["current_output_mw"] = float(point.output_mw)
        for bid, point in interval.batteries.items():
            for row in state["batteries"]:
                if str(row["id"]) != str(bid):
                    continue
                power = float(point.power_mw)
                efficiency = float(row["efficiency"])
                if power >= 0:
                    energy_delta = power * dt_h / efficiency
                else:
                    energy_delta = power * dt_h * efficiency
                row["soc_pct"] = max(
                    0.0,
                    min(100.0, float(row["soc_pct"]) - energy_delta / float(row["capacity_mwh"]) * 100.0),
                )
                row["current_power_mw"] = power


def main() -> None:
    policy = load_json(ROOT / "config" / "decision_policy_v1.json")
    configured = [Strategy(value) for value in policy["strategies"]]

    expectations = {
        "normal": {
            Strategy.SOLAR_ONLY: "FEASIBLE",
            Strategy.BATTERY_ONLY: "FEASIBLE",
            Strategy.MIXED: "FEASIBLE",
            Strategy.COST_MIN: "FEASIBLE",
        },
        "problem": {
            Strategy.SOLAR_ONLY: "FEASIBLE",
            Strategy.BATTERY_ONLY: "INFEASIBLE",
            Strategy.MIXED: "FEASIBLE",
            Strategy.COST_MIN: "FEASIBLE",
        },
    }

    for dataset_name in ("normal", "problem"):
        data = load_json(ROOT / "datasets" / f"{dataset_name}.json")
        generators, batteries = objects_for(data)
        snapshot = snapshot_for(data)
        series = planning_series(data)

        plant_sum = sum(float(row["current_output_mw"]) for row in data["generators"])
        snapshot_solar = float(data["snapshot"]["solar_gen_mw"])
        assert abs(plant_sum - snapshot_solar) <= 1e-6, (
            f"{dataset_name}: snapshot solar {snapshot_solar} != generator sum {plant_sum}"
        )

        print(f"[{dataset_name}]")
        for strategy in configured:
            built, _ = _build_candidate(
                snapshot,
                generators,
                batteries,
                series,
                strategy,
                policy,
                120,
                [],
                float(data["reserve_assessment"]["required_reserve_mw"]),
            )
            expected = expectations[dataset_name][strategy]
            actual = built["optimization_status"]
            assert actual == expected, f"{dataset_name}/{strategy.value}: expected {expected}, got {actual}"
            if actual == "FEASIBLE":
                residual = final_balance(built, series[-1]["demand"])
                assert abs(residual) <= 1e-6, f"{dataset_name}/{strategy.value}: residual={residual}"
                print(f"  {strategy.value:12s} FEASIBLE final residual={residual:.6f} MW")
            else:
                print(f"  {strategy.value:12s} INFEASIBLE (expected)")

    # Repeated-plan regression: execute a feasible MIXED problem plan exactly,
    # persist its final state, then verify a second planning cycle still has
    # feasible generator-capable recovery options instead of accumulating solar.
    data = load_json(ROOT / "datasets" / "problem.json")
    generators, batteries = objects_for(data)
    state = {
        "generators": copy.deepcopy(data["generators"]),
        "batteries": copy.deepcopy(data["batteries"]),
    }
    snapshot = snapshot_for(data, state)
    series = planning_series(data)
    first, _ = _build_candidate(
        snapshot, generators, batteries, series, Strategy.MIXED, policy, 120, [],
        float(data["reserve_assessment"]["required_reserve_mw"]),
    )
    assert first["optimization_status"] == "FEASIBLE"
    apply_dispatch_to_state(state, first["dispatch"])
    post_solar = sum(float(row["current_output_mw"]) for row in state["generators"])
    post_demand = float(series[-1]["demand"])
    post_snapshot = SimpleNamespace(state_json=state, solar_gen_mw=post_solar, timestamp=datetime(2026, 1, 1))
    persistence = [{"solar": post_solar, "demand": post_demand} for _ in range(8)]

    for strategy in (Strategy.SOLAR_ONLY, Strategy.MIXED, Strategy.COST_MIN):
        second, _ = _build_candidate(
            post_snapshot, generators, batteries, persistence, strategy, policy, 120, [],
            0.1 * post_demand,
        )
        assert second["optimization_status"] == "FEASIBLE", (
            f"repeat problem/{strategy.value}: {second['optimization_status']}"
        )
        residual = final_balance(second, post_demand)
        assert abs(residual) <= 1e-6
    print("[repeat problem] generator-capable second planning cycle remains FEASIBLE")
    print("PASS: planning/execution balance semantics are consistent.")


if __name__ == "__main__":
    main()
