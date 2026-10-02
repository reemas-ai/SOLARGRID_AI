# SolarGrid AI ⚡

**Autonomous AI-assisted solar-grid planning, validation, safety, execution, monitoring, and adaptive energy management.**

SolarGrid AI is a synthetic solar-grid control platform designed to demonstrate how an AI planning agent can reason over grid state, use deterministic engineering tools, validate candidate plans, enforce safety and human-approval boundaries, execute simulated actions, monitor their outcomes, and learn from verified operational results.

The system combines **LLM-assisted planning** with **deterministic engineering tools and safety controls**, so the AI does not directly bypass the engineering validation or execution boundaries.

---

## Key Capabilities

* AI-assisted planning through a bounded tool-calling workflow
* Deterministic reserve, imbalance, risk, constraint, power-flow, and plan evaluation
* Human approval before execution
* Safety validation before Tool 13 execution
* Simulated execution with expected-vs-actual outcome verification
* Post-execution monitoring and revalidation
* Operational memory and lesson learning
* RAG-based engineering evidence
* Event-driven/autonomous monitoring
* External-data provenance and source-quality tracking
* Solar fleet and network simulation
* Dynamic Pricing for solar-surplus conditions
* Browser-based Control Center UI
* Automated regression and contract verification tests

---

## Architecture

```text
                    ┌──────────────────────┐
                    │     Control Center   │
                    │       Web UI         │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │     Planner Agent     │
                    │  bounded tool loop    │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │      Dispatcher       │
                    │   Tools 01 → 17       │
                    └──────────┬───────────┘
                               │
             ┌─────────────────┼──────────────────┐
             ▼                 ▼                  ▼
       State & Risk        Planning &        Operations &
       Assessment          Validation        Monitoring
             │                 │                  │
             └─────────────────┼──────────────────┘
                               ▼
                    ┌──────────────────────┐
                    │   Safety + Approval   │
                    │    execution gate     │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │ Simulated Execution   │
                    │      Tool 13          │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │ Outcome Verification  │
                    │ Memory / Monitoring   │
                    └──────────────────────┘
```

The architecture deliberately separates AI reasoning from deterministic engineering validation and from the final execution boundary.

---

## Tool Architecture

SolarGrid currently exposes **17 real tools** through the public tool registry and dispatcher.

| Tool | Capability                     |
| ---- | ------------------------------ |
| 01   | System State                   |
| 02   | Reserve Assessment             |
| 03   | Imbalance Detection            |
| 04   | Future Risk Assessment         |
| 05   | Plan Generation / Optimization |
| 06   | Generator Constraints          |
| 07   | Battery Constraints            |
| 08   | Power Flow                     |
| 09   | Plan Evaluation                |
| 10   | Scenario Analysis              |
| 11   | Monitoring                     |
| 12   | Change Impact Analysis         |
| 13   | Execute / Verify               |
| 14   | Diagnosis                      |
| 15   | Forecast Error                 |
| 16   | RAG Engineering Evidence       |
| 17   | Dynamic Pricing                |

---

## Safety and Human Approval

The AI planning loop does not directly execute grid actions.

The lifecycle is:

```text
Assess
  ↓
Generate Plans
  ↓
Deterministic Validation
  ↓
Plan Evaluation
  ↓
Human Approval
  ↓
Safety Gate
  ↓
Simulated Execution
  ↓
Outcome Verification
  ↓
Monitoring / Learning
```

The autonomous monitoring workflow can assess the system and produce planning/validation results, but **human approval and the safety boundary remain required before execution**.

---

## Dynamic Pricing

Tool 17 provides a native Dynamic Pricing capability for solar-surplus conditions.

It uses persisted system state, solar forecasts, load, and battery information to:

1. Detect solar-surplus intervals.
2. Calculate a deterministic price signal.
3. Estimate flexible-load response.
4. Build an energy-conserving load-shift opportunity.
5. Pass the persisted Tool 17 analysis into the existing planning lifecycle.

Dynamic Pricing does not create a separate execution or approval subsystem.

Its lifecycle reuses the existing architecture:

```text
Tool 17 Dynamic Pricing
        ↓
Tool 05 Plan Generation
        ↓
Tools 06–09 Validation / Evaluation
        ↓
Human Approval
        ↓
Safety Gate
        ↓
Tool 13 Simulated Execution
        ↓
Tool 14 Outcome Verification
        ↓
Lesson Memory
```

---

## RAG / Engineering Evidence

SolarGrid includes a synthetic engineering evidence corpus under:

```text
RAG/docs/
```

The corpus covers topics including:

* Synthetic grid operating rules
* Generator technical requirements
* Battery engineering
* Renewable forecasting
* Network power-flow limits
* Operating reserve
* Plan-change impact
* Engineering validation and safety

Tool 16 retrieves evidence from this corpus and exposes the supporting evidence to the planning/safety workflow.

---

## Autonomous Monitoring

The project includes an event-driven monitoring layer under:

```text
automation/
```

The monitoring service can:

* detect operational changes,
* assess future risk,
* trigger planning workflows,
* preserve trigger provenance,
* and route the resulting workflow through the existing validation and approval boundaries.

Monitoring does not bypass the human-approval or safety requirements.

---

## External Data

External-data adapters are available under:

```text
external_data/
```

The project includes adapters/provenance handling for sources such as:

* Open-Meteo
* NASA POWER
* EIA configuration

The demo remains a **synthetic grid environment**. External sources are used for supported data/provenance workflows and are not presented as live Jordan grid telemetry.

---

## Demo Datasets

Three demo scenarios are included:

```text
datasets/
├── normal.json
├── problem.json
└── dynamic_pricing.json
```

### Normal

Stable operating conditions for the standard planning workflow.

### Problem

A stressed synthetic scenario designed to exercise conditions such as:

* solar generation loss,
* increased demand,
* low battery state,
* reserve shortage,
* and network constraints.

### Dynamic Pricing

A dedicated solar-surplus scenario for demonstrating Tool 17 and flexible-load response.

---

## Requirements

Recommended runtime:

**Python 3.12.x**

Install dependencies:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

Create the local environment file:

```powershell
copy .env.example .env
```

Add private API keys to `.env` only when required.

**Never commit `.env` or private API keys.**

---

## Running the Demo

Load the normal scenario:

```powershell
python scripts/load_demo_dataset.py normal
```

Load the problem scenario:

```powershell
python scripts/load_demo_dataset.py problem
```

Load the Dynamic Pricing scenario:

```powershell
python scripts/load_demo_dataset.py dynamic_pricing
```

Start the application:

```powershell
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

---

## Verification

Run the full regression suite:

```powershell
pytest -q tests
```

Useful targeted verification scripts include:

```powershell
python scripts/verify_dynamic_pricing_integration.py
python scripts/verify_dynamic_pricing_dataset.py
python scripts/verify_planning_execution_consistency.py
python scripts/verify_map_status_contract.py
python scripts/runtime_preflight.py
```

---

## Project Structure

```text
agents/          AI agents, planner, memory, safety, dispatcher
automation/      Event-driven monitoring
config/          Runtime and decision configuration
datasets/        Synthetic demonstration scenarios
external_data/   External-data adapters and provenance
network/         Network / simulation benchmarks
RAG/             Engineering evidence corpus and retrieval
scripts/         Setup and verification utilities
static/          Frontend assets
templates/       Flask UI templates
tests/           Regression and contract tests
tools/            Tools 01–17
```

Core application modules include:

```text
app.py
database.py
runtime.py
schemas.py
simulation.py
tool_registry.py
```

---

## Data and Safety Boundary

SolarGrid is a **synthetic grid-control demonstration**.

Operational values, plant outputs, forecasts, network conditions, and execution results in the demo are simulated or derived from configured synthetic datasets unless explicitly identified as external-source data.

The system is not intended to represent live control of a real electrical grid.

---

## Demo Focus

The recommended demonstration flow is:

```text
Grid State
   ↓
Risk / Reserve Assessment
   ↓
Generate Plans
   ↓
Validate
   ↓
Human Approval
   ↓
Safety Check
   ↓
Execute
   ↓
Verify Outcome
   ↓
Monitor
   ↓
Replan / Learn when required
```

Dynamic Pricing can then be demonstrated as an additional intelligent energy-management capability using the same controlled lifecycle.
