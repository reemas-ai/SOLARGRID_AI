# SolarGrid Demo Datasets

The project ships with three deterministic synthetic operating datasets. Loading either dataset **resets the demo database** so the scenario starts from a clean state.

Both datasets use the same six Jordan solar-project identities as actual `Generator` rows:

1. Shams Ma'an Solar Power Plant
2. Quweira Solar Power Plant
3. Baynouna Solar Energy Project
4. ACWA Sunrise Al Mafraq Solar PV
5. Falcon Ma'an Solar Project
6. AM Solar Project

The project identities/nameplate capacities are reference metadata. Output, operating ceilings, ramp rates, availability, forecasts and all execution values are synthetic demo data, not live utility telemetry.

## Normal scenario

```powershell
python scripts/load_demo_dataset.py normal
```

Source: `datasets/normal.json`

Expected characteristics:

- stable solar-only operating state
- all six solar projects available / GREEN
- fleet output = 75 MW
- demand = 75 MW
- balanced current condition
- sufficient reserve
- low future risk
- all six sites participate in reserve, planning, execution, monitoring and the map

Use this scenario to test the complete happy path: assessment → plan generation → approval → execution → post-execution reserve assessment → goal verification → memory.

## Problem scenario — recoverable

```powershell
python scripts/load_demo_dataset.py problem
```

Source: `datasets/problem.json`

Expected characteristics:

- Shams Ma'an is `TRIPPED` / RED / 0 MW
- Falcon Ma'an is `DEGRADED` / YELLOW / 0 MW
- Quweira, Baynouna, ACWA Mafraq and AM Solar remain AVAILABLE / GREEN
- available solar fleet output = 44 MW
- current demand = 62 MW
- current deficit is approximately 18 MW
- grid line loading is elevated but not deliberately beyond the hard congestion limit
- battery SOC is moderate rather than critically low
- operating reserve remains sufficient for the approved demo policy
- future risk is elevated (`MEDIUM`) but the scenario is intentionally recoverable
- `SOLAR_ONLY`, `MIXED`, and `COST_MIN` are expected to produce `FEASIBLE` recovery plans
- `BATTERY_ONLY` is expected to remain `INFEASIBLE` because the 120-minute energy requirement exceeds usable battery energy
- the four AVAILABLE solar projects retain enough synthetic operating headroom for a second planning cycle after a successful execution

Use this scenario to demonstrate the full recovery workflow:

```text
Problem detected
→ Assessment
→ Generate Plans
→ at least one FEASIBLE plan
→ Human Approval
→ Safety Gate
→ Execute
→ Post-Execution Reserve Assessment
→ Goal Verification
→ Operational Outcome
→ Memory / Lessons
```

The problem scenario is **not** intended to be a deliberately unrecoverable/critical safety-block scenario.

Both datasets use the same 120-minute / 15-minute solar-and-demand forecast contract and remain synthetic course-demo data.


## Dynamic Pricing scenario — optional feature demo

```powershell
python scripts/load_demo_dataset.py dynamic_pricing
```

Source: `datasets/dynamic_pricing.json`

This optional scenario leaves the established `normal` and `problem` datasets unchanged. It is specifically designed to demonstrate the integrated **Tool 17 Dynamic Pricing** workflow while remaining solar-only:

- current snapshot remains stable and solar-only;
- the synthetic battery starts at its configured maximum SOC, so no extra charging headroom is assumed;
- one synthetic system load is explicitly marked `is_flexible=True`;
- the 120-minute forecast contains solar-surplus target intervals;
- Tool 17 can calculate a deterministic discounted price and conserved source-to-target load shift;
- resulting plans still go through Tools 05–09, human approval, Safety, Tool 13, Tool 14 and Memory like every other SolarGrid Plan.

Verify the dataset without requiring Flask with:

```powershell
python scripts/verify_dynamic_pricing_dataset.py
```

Use this dataset when you want the UI to show an **ACTION AVAILABLE** Dynamic Pricing example. The normal/problem datasets remain regression baselines and may correctly return `NO_ACTION` when no solar-surplus/flexible-load opportunity exists.

## Solar generation semantics

The demo uses one authoritative definition of solar generation:

```text
snapshot.solar_gen_mw = sum of actual solar generator outputs
```

The aggregate `solar` forecast describes the same solar fleet. It is **not** added on top of generator dispatch. This prevents solar production from being counted twice during planning and post-execution verification.

## Capacity semantics

For each project:

- `capacity_mw` = project reference nameplate capacity.
- `max_output_mw` = synthetic operating ceiling available to the demo optimizer under the current scenario.

This separation prevents the optimizer from treating full nameplate capacity as instantly available solar output regardless of current conditions.
