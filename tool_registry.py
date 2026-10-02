"""SOLARGRID AI public tool registry for grouped 01-17 architecture."""
from tools.state_tools import get_system_state, calculate_reserve, detect_imbalance, assess_future_risk
from tools.planning_tools import generate_and_optimize_plans, check_generator_constraints, check_battery_constraints, run_power_flow, evaluate_plans
from tools.operational_tools import run_scenario_analysis, monitor_system_conditions, assess_change_impact, execute_and_verify_plan, assess_and_diagnose_outcome, forecast_error_analysis, retrieve_engineering_evidence
from tools.dynamic_pricing_tools import analyze_dynamic_pricing

TOOLS_01_17 = {
    "get_system_state": get_system_state,
    "calculate_reserve": calculate_reserve,
    "detect_imbalance": detect_imbalance,
    "assess_future_risk": assess_future_risk,
    "generate_and_optimize_plans": generate_and_optimize_plans,
    "check_generator_constraints": check_generator_constraints,
    "check_battery_constraints": check_battery_constraints,
    "run_power_flow": run_power_flow,
    "evaluate_plans": evaluate_plans,
    "run_scenario_analysis": run_scenario_analysis,
    "monitor_system_conditions": monitor_system_conditions,
    "assess_change_impact": assess_change_impact,
    "execute_and_verify_plan": execute_and_verify_plan,
    "assess_and_diagnose_outcome": assess_and_diagnose_outcome,
    "forecast_error_analysis": forecast_error_analysis,
    "retrieve_engineering_evidence": retrieve_engineering_evidence,
    "analyze_dynamic_pricing": analyze_dynamic_pricing,
}

# Compatibility view: the original Tools 01-16 remain addressable under the
# historic collection name.  Tool 17 is added without renumbering or mutating
# any existing tool identity.
TOOLS_01_16 = {k: v for k, v in TOOLS_01_17.items() if k != "analyze_dynamic_pricing"}
TOOLS_01_09 = {k:v for k,v in TOOLS_01_16.items() if k not in {"run_scenario_analysis","monitor_system_conditions","assess_change_impact","execute_and_verify_plan","assess_and_diagnose_outcome","forecast_error_analysis","retrieve_engineering_evidence"}}

def get_tool(name: str):
    try: return TOOLS_01_17[name]
    except KeyError as exc: raise KeyError(f"Unknown SOLARGRID tool: {name}") from exc
