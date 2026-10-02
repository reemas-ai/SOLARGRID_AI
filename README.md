# SolarGrid AI ⚡

**Autonomous AI-assisted solar-grid planning, validation, safety, execution, monitoring, and adaptive energy management.**

SolarGrid AI is an AI-assisted energy-management and planning system designed to demonstrate how intelligent agents can work with deterministic engineering tools to plan, validate, safely approve, execute, monitor, and adapt solar-grid operations.

The system combines **LLM-assisted planning**, **deterministic engineering validation**, **safety controls**, **human approval**, **operational monitoring**, **RAG-based engineering evidence**, **memory**, and **dynamic pricing** into one end-to-end workflow.

---

## Key Capabilities

* 🤖 **AI-assisted planning** with a bounded tool-calling workflow
* ⚡ **Deterministic grid calculations** for engineering validation
* 🛡️ **SafetyAgent** for safety and approval decisions
* 👤 **Human approval boundary** before operational execution
* 🔄 **Plan lifecycle management** from generation through execution and monitoring
* 📊 **Grid state, reserve, imbalance, risk, and forecast analysis**
* 🔍 **Plan evaluation and validation**
* 🧠 **Operational memory** for learning from previous outcomes
* 📚 **RAG-based engineering evidence retrieval**
* ⏱️ **Autonomous monitoring and revalidation**
* 💰 **Dynamic pricing** based on energy surplus conditions
* 📈 **Forecast-error analysis**
* 🧪 **Scenario analysis**
* 🔧 **Generator and battery constraint handling**
* 🌐 **External energy-data integration**
* 🧩 **17 registered operational tools**

---

# Architecture

```text
                    ┌───────────────────────┐
                    │       User / UI       │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │      Planner Agent    │
                    │   LLM-assisted loop   │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │    Tool Dispatcher    │
                    │  bounded tool calls   │
                    └───────────┬───────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
       ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
       │ Grid / State│   │ Planning &  │   │ Safety /    │
       │   Tools     │   │ Evaluation  │   │ Monitoring  │
       └─────────────┘   └─────────────┘   └─────────────┘
              │                 │                 │
              └─────────────────┼─────────────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │    SafetyAgent        │
                    │  + Human Approval     │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │ Execution / Verify    │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │ Monitoring / Outcome  │
                    │ Memory / Revalidation │
                    └───────────────────────┘
```

The architecture intentionally separates **AI reasoning** from **deterministic engineering operations**.

The LLM can select and orchestrate tools, while the engineering tools perform structured calculations and validation. Safety controls and human approval remain explicit boundaries before operational execution.

---

# Tool Architecture

SolarGrid AI currently exposes **17 operational tools** through the project tool registry and dispatcher.

| #  | Tool                  | Purpose                                               |
| -- | --------------------- | ----------------------------------------------------- |
| 01 | State                 | Retrieve and analyze the current grid state           |
| 02 | Reserve               | Calculate available operational reserve               |
| 03 | Imbalance             | Detect current grid imbalance                         |
| 04 | Risk                  | Assess future operational risk                        |
| 05 | Plan Generation       | Generate candidate operational plans                  |
| 06 | Generator Constraints | Validate generator operating constraints              |
| 07 | Battery Constraints   | Validate battery operating constraints                |
| 08 | Power Flow            | Perform power-flow analysis                           |
| 09 | Evaluate              | Evaluate candidate plans                              |
| 10 | Scenario              | Analyze operational scenarios                         |
| 11 | Monitor               | Monitor plan execution and grid changes               |
| 12 | Change Impact         | Determine whether changes require replanning          |
| 13 | Execute / Verify      | Execute simulated plans and verify outcomes           |
| 14 | Diagnosis             | Diagnose operational conditions                       |
| 15 | Forecast Error        | Analyze forecast deviations                           |
| 16 | RAG Evidence          | Retrieve supporting engineering evidence              |
| 17 | Dynamic Pricing       | Calculate adaptive pricing based on energy conditions |

The dispatcher provides a common execution path and traceability across tool calls.

---

# Safety and Human Approval

Safety is intentionally separated from autonomous planning.

The operational lifecycle is:

```text
Plan
  │
  ▼
Validate
  │
  ▼
Safety Assessment
  │
  ├──────────────► BLOCK
  │
  ├──────────────► REQUIRES HUMAN APPROVAL
  │
  ▼
Approve
  │
  ▼
Execute
  │
  ▼
Verify
  │
  ▼
Monitor
  │
  ▼
Revalidate / Replan when required
```

The system is designed so that an AI-generated plan is not automatically treated as an approved operational action.

Safety decisions and the human approval boundary remain explicit parts of the workflow.

---

# Dynamic Pricing

SolarGrid AI includes a dynamic-pricing capability designed around changing energy surplus.

The concept is:

```text
Higher Energy Surplus
        │
        ▼
 Lower Energy Price
        │
        ▼
 Encourage Charging / Energy Use
        │
        ▼
 Absorb Available Surplus
```

When surplus conditions decrease, the pricing signal can return toward the normal level.

The feature is integrated into the tool architecture as **Tool 17 — Dynamic Pricing** and is designed to support adaptive energy-management workflows.

---

# RAG / Engineering Evidence

SolarGrid AI includes a Retrieval-Augmented Generation layer for engineering evidence.

The RAG subsystem provides supporting information to the planning and safety workflow rather than replacing deterministic engineering validation.

The project includes an engineering-oriented evidence corpus containing reference material related to areas such as:

* Solar and renewable-energy systems
* Grid operation
* Power systems
* Battery / energy-storage operation
* Forecasting
* Energy management
* Operational constraints

The architecture supports evidence retrieval, source tracking, and fail-closed behavior when required evidence is unavailable.

---

# Autonomous Monitoring

The system includes an autonomous monitoring workflow for detecting operational changes after plan approval.

The monitoring lifecycle is:

```text
Approved Plan
     │
     ▼
Monitor Current State
     │
     ▼
Detect Change
     │
     ├── No Significant Change
     │        │
     │        ▼
     │      Continue
     │
     └── Change Detected
              │
              ▼
        Change Impact Analysis
              │
              ▼
       Revalidation / Replanning
```

Monitoring results can identify changes that may invalidate an approved plan and trigger the appropriate revalidation or replanning workflow.

---

# External Data

SolarGrid AI supports external energy and weather data sources used by the simulation and planning workflow.

The project architecture includes integrations and references for external datasets such as:

* Open-Meteo
* NASA POWER
* Energy-related reference data

External data is kept separate from the deterministic planning and validation layers so that data provenance can be tracked independently.

---

# Demo Datasets

The repository includes project datasets and simulation resources used to demonstrate the planning workflow.

These datasets support scenarios involving:

* Solar generation
* Load demand
* Battery operation
* Grid conditions
* Forecasts
* Reserve
* Imbalance
* Operational risk
* Plan evaluation
* Monitoring
* Dynamic pricing

The project is designed to operate within a controlled simulation environment rather than directly controlling physical grid infrastructure.

---

# Requirements

## Python

The project targets:

```text
Python 3.12.x
```

The repository includes `.python-version` to communicate the intended Python runtime.

## Dependencies

Install the project dependencies with:

```bash
pip install -r requirements.txt
```

For the most reproducible environment, use the Python version specified by the project configuration.

---

# Running the Demo

Clone the repository and enter the project directory:

```bash
git clone https://github.com/reemas-ai/SOLARGRID_AI.git
cd SOLARGRID_AI
```

Create the local environment configuration:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Then run the application according to the configured project entry point.

The repository includes the application, simulation, planning, tool, monitoring, and test components required for the demonstration workflow.

---

# Verification

The project includes a test suite covering important components of the architecture.

Tests cover areas including:

* Tool registration
* Tool dispatching
* Run / trace propagation
* Planner integration
* Plan generation
* Reserve handling
* Plan evaluation
* Safety decisions
* Monitoring
* Change impact
* Execution / verification
* Memory
* RAG evidence
* Dynamic pricing
* Operational workflows

Run the test suite with:

```bash
pytest
```

The repository also contains dedicated test and validation resources for the implemented architecture.

---

# Project Structure

```text
SOLARGRID_AI/
│
├── agents/
│   ├── planner / planning logic
│   ├── safety logic
│   ├── memory
│   └── agent workflows
│
├── automation/
│   └── monitoring / autonomous workflows
│
├── config/
│   └── project configuration
│
├── datasets/
│   └── demonstration datasets
│
├── external_data/
│   └── external-data resources
│
├── RAG/
│   ├── rag engine
│   └── engineering evidence
│
├── scripts/
│   └── project utilities
│
├── simulation/
│   └── simulation resources
│
├── solar_projects/
│   └── solar-grid project data
│
├── static/
│   └── frontend assets
│
├── templates/
│   └── application templates
│
├── tests/
│   └── automated tests
│
├── tools/
│   └── operational tools
│
├── tool_registry.py
│   └── registered tool architecture
│
├── app.py
│   └── application entry point
│
├── database.py
│   └── database layer
│
├── schemas.py
│   └── structured data schemas
│
├── runtime.py
│   └── runtime configuration
│
├── simulation.py
│   └── simulation entry points
│
├── requirements.txt
│   └── Python dependencies
│
├── .env.example
│   └── environment configuration template
│
├── .gitignore
│   └── ignored local/runtime files
│
└── .python-version
    └── target Python version
```

---

# Data and Safety Boundary

SolarGrid AI is a **simulation and decision-support system**.

It is designed to demonstrate AI-assisted planning and operational decision workflows. It does not directly control physical electrical infrastructure through this repository.

Operational actions are represented within the project's controlled execution and verification workflow.

The system maintains a separation between:

* AI-generated recommendations
* Deterministic engineering calculations
* Safety validation
* Human approval
* Simulated execution
* Monitoring and revalidation

This separation is a core part of the project architecture.

---

# Demo Focus

The project demonstration focuses on the complete operational loop:

```text
Observe
  ↓
Analyze
  ↓
Plan
  ↓
Validate
  ↓
Assess Safety
  ↓
Human Approval
  ↓
Execute / Verify
  ↓
Monitor
  ↓
Learn / Revalidate
  ↓
Adapt
```

The goal is to demonstrate how AI-assisted energy management can be combined with deterministic engineering logic, safety controls, human oversight, and continuous operational feedback in a single architecture.

---

## Project

**SolarGrid AI**

AI-assisted autonomous solar-grid planning and adaptive energy management.

Repository:

https://github.com/reemas-ai/SOLARGRID_AI
