"""SOLARGRID AI Planner control plane.

The Planner is an orchestration boundary. Domain execution always goes through
agents.tool_dispatcher.execute_tool; the Planner does not import individual
domain tools and does not implement physical calculations or safety policy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from agents.prompts import PLANNER_SYSTEM_PROMPT
from agents.tool_dispatcher import ToolContext, execute_tool, REQUEST_SCHEMAS
from plan_lifecycle import transition_plan
from database import Plan, SystemSnapshot, ReserveAssessment
from config_loader import load_decision_policy
from schemas import PlanDispatch
from agents.safety_agent import SafetyAgent, SafetyDecision
from agents.memory import SystemMemoryManager

@dataclass
class PlannerLimits:
    max_steps: int = 32
    max_replans: int = 3
    max_tool_calls: int = 16


@dataclass
class PlannerAgent:
    context: ToolContext
    limits: PlannerLimits = field(default_factory=PlannerLimits)
    llm: Callable[..., Any] | None = None
    _steps_used: int = field(default=0, init=False, repr=False)
    _replans_used: int = field(default=0, init=False, repr=False)
    _tool_calls_used: int = field(default=0, init=False, repr=False)

    def _call(self, name: str, payload: dict[str, Any]) -> Any:
        if self.limits.max_steps <= 0:
            raise RuntimeError("Planner max_steps must be positive")
        if self._steps_used >= self.limits.max_steps:
            raise RuntimeError(f"Planner step limit exceeded: {self.limits.max_steps}")
        if self._tool_calls_used >= self.limits.max_tool_calls:
            raise RuntimeError(f"Planner tool-call limit exceeded: {self.limits.max_tool_calls}")
        self._steps_used += 1
        self._tool_calls_used += 1
        return execute_tool(name, payload, context=self.context)

    @staticmethod
    def tool_definitions() -> list[dict[str, Any]]:
        """Generate LLM tool definitions from the authoritative request schemas."""
        definitions = []
        for name, schema in REQUEST_SCHEMAS.items():
            description = (schema.__doc__ or "").strip() or f"Execute SOLARGRID tool: {name}."
            definitions.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": schema.model_json_schema(),
                },
            })
        return definitions

    def run_llm_tool_loop(
    self,
    user_message: str,
    *,
    llm: Callable[..., Any] | None = None,
    max_iterations: int | None = None,
    current_snapshot_id: int | None = None,
) -> str:
        """Run a bounded OpenAI-compatible tool-calling loop.

        A Mock LLM can be injected for deterministic tests. Tool execution is
        always routed through the central Dispatcher with the same ToolContext.
        """
        model = llm or self.llm
        if model is None:
            raise RuntimeError("No LLM callable supplied")
        iterations = max_iterations if max_iterations is not None else self.limits.max_steps
        if iterations <= 0:
            raise RuntimeError("max_iterations must be positive")
        messages: list[dict[str, Any]] = [
                {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            ]

        if current_snapshot_id is not None:
                messages.append({
                    "role": "system",
                    "content": self._build_memory_context(current_snapshot_id),
                })

        messages.append({"role": "user", "content": user_message})
        tools = self.tool_definitions()

        for _ in range(iterations):
            response = model(messages=messages, tools=tools, tool_choice="auto")
            message = getattr(getattr(response, "choices", [None])[0], "message", None)
            if message is None:
                raise RuntimeError("LLM returned no message")

            tool_calls = getattr(message, "tool_calls", None) or []
            content = getattr(message, "content", None)
            assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": getattr(tc, "id", None),
                        "type": "function",
                        "function": {
                            "name": getattr(getattr(tc, "function", None), "name", ""),
                            "arguments": getattr(getattr(tc, "function", None), "arguments", "{}"),
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_message)

            if not tool_calls:
                return content or ""

            for tc in tool_calls:
                if self._tool_calls_used >= self.limits.max_tool_calls:
                    raise RuntimeError(f"Planner tool-call limit exceeded: {self.limits.max_tool_calls}")
                fn = getattr(tc, "function", None)
                name = getattr(fn, "name", None)
                raw_args = getattr(fn, "arguments", "{}")
                if not name:
                    raise RuntimeError("LLM returned a tool call without a tool name")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"Invalid tool-call JSON for {name}: {exc}") from exc
                result = self._call(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": getattr(tc, "id", ""),
                    "name": name,
                    "content": json.dumps(result, default=str),
                })

        raise RuntimeError(f"Planner LLM loop exceeded max_iterations={iterations}")

    def _build_memory_context(self, snapshot_id: int) -> str:
        """Build factual operational-memory context for a known current snapshot.

        The current snapshot is authoritative for present-state values. Memory
        contributes only historical actions, outcomes, lessons, and current-run
        trace information. Historical values must never be presented as the
        current state.
        """
        snapshot = self.context.db.session.get(SystemSnapshot, snapshot_id)

        if snapshot is None:
            raise LookupError(
                f"SystemSnapshot with id={snapshot_id} does not exist."
            )

        memory = SystemMemoryManager()

        current_conditions = {
            field_name: getattr(snapshot, field_name)
            for field_name in (
                "demand_mw",
                "solar_gen_mw",
                "other_gen_mw",
                "battery_soc_pct",
                "reserve_margin_mw",
            )
            if getattr(snapshot, field_name) is not None
        }

        if snapshot.grid_status is not None:
            current_conditions["grid_status"] = snapshot.grid_status

        sections = [
            "OPERATIONAL MEMORY CONTEXT",
            (
                "The following current-state values come from the Current verified "
                "SystemSnapshot and are authoritative for the present "
                "situation."
            ),
            (
                f"Current snapshot: id={snapshot.id}; "
                f"timestamp={snapshot.timestamp}; "
                f"demand_mw={snapshot.demand_mw}; "
                f"solar_gen_mw={snapshot.solar_gen_mw}; "
                f"other_gen_mw={snapshot.other_gen_mw}; "
                f"battery_soc_pct={snapshot.battery_soc_pct}; "
                f"reserve_margin_mw={snapshot.reserve_margin_mw}; "
                f"grid_status={snapshot.grid_status}"
            ),
            (
                "Historical memory is evidence about what happened before. "
                "It must not override the current snapshot or current tool "
                "results."
            ),
            "Relevant historical cases:",
            memory.search_similar_cases(snapshot_id, "both"),
            "Relevant active lessons:",
            memory.get_active_lessons(current_conditions),
        ]

        if self.context.run_id is not None:
            sections.extend([
                "Detailed Current Run History (short-term execution trace):",
                memory.get_run_history(
                    self.context.run_id,
                    include_details=True,
                ),
            ])

        return "\n\n".join(sections)

    def resolve_validation_context(
            self,
            *,
            snapshot_id: int,
            horizon_minutes: int,
            decision_policy_version: str = "v1.0-demo",
            reserve_assessment_id: int | None = None,
            ensure_reserve: bool = True,
        ) -> dict[str, Any]:
        """Resolve one deterministic validation context for Tools 05-09.

        Planning used to let Tool 05, Tool 08 and Tool 09 independently infer
        missing context. That made the same candidate appear valid in one stage
        and UNKNOWN in another. The Planner now resolves the approved network
        case and the horizon-matched reserve assessment once and passes those
        identifiers through every validation stage.
        """
        snapshot = self.context.db.session.get(SystemSnapshot, snapshot_id)
        if snapshot is None:
            raise LookupError(f"SystemSnapshot with id={snapshot_id} does not exist.")

        policy = load_decision_policy(decision_policy_version)
        network_id = policy.get("network_id")
        if bool(policy.get("network_validation_required")) and not network_id:
            raise ValueError("Approved decision policy requires network validation but has no network_id")

        reserve = None
        if reserve_assessment_id is not None:
            reserve = self.context.db.session.get(ReserveAssessment, int(reserve_assessment_id))
            if reserve is None:
                raise LookupError(f"ReserveAssessment with id={reserve_assessment_id} does not exist.")
            if int(reserve.snapshot_id) != int(snapshot_id):
                raise ValueError("ReserveAssessment does not belong to the planning snapshot")
            if reserve.time_horizon_minutes is None or abs(float(reserve.time_horizon_minutes) - float(horizon_minutes)) > 1e-9:
                raise ValueError("ReserveAssessment horizon does not match the planning horizon")
        else:
            matches = (
                ReserveAssessment.query
                .filter_by(snapshot_id=snapshot_id)
                .order_by(ReserveAssessment.id.desc())
                .all()
            )
            reserve = next((
                item for item in matches
                if item.time_horizon_minutes is not None
                and abs(float(item.time_horizon_minutes) - float(horizon_minutes)) <= 1e-9
                and item.required_reserve_mw is not None
                and item.actual_reserve_mw is not None
            ), None)

        if reserve is None and ensure_reserve:
            calculated = self._call("calculate_reserve", {
                "snapshot_id": snapshot_id,
                "horizon_minutes": horizon_minutes,
            })
            calculated_data = _read(calculated, "data", {}) or {}
            new_id = calculated_data.get("assessment_id") if isinstance(calculated_data, dict) else None
            if new_id is not None:
                reserve = self.context.db.session.get(ReserveAssessment, int(new_id))

        return {
            "snapshot_id": snapshot_id,
            "horizon_minutes": horizon_minutes,
            "reserve_assessment_id": reserve.id if reserve is not None else None,
            "network_id": network_id,
            "decision_policy_version": decision_policy_version,
        }

    def assess_current_state(self, *, horizon_minutes: int = 120, external_weather_record_id: int | None = None) -> dict[str, Any]:
        payload = {"horizon_minutes": horizon_minutes}
        if external_weather_record_id is not None:
            payload["external_weather_record_id"] = int(external_weather_record_id)
        state = self._call("get_system_state", payload)
        snapshot_id = _extract_snapshot_id(state)
        if snapshot_id is None:
            raise RuntimeError("Tool 01 did not return a usable snapshot_id")
        reserve = self._call("calculate_reserve", {"snapshot_id": snapshot_id, "horizon_minutes": horizon_minutes})
        imbalance = self._call("detect_imbalance", {"snapshot_id": snapshot_id})
        risk = self._call("assess_future_risk", {"snapshot_id": snapshot_id, "horizon_minutes": horizon_minutes})
        return {"snapshot_id": snapshot_id, "state": state, "reserve": reserve, "imbalance": imbalance, "future_risk": risk}

    def analyze_dynamic_pricing(self, *, snapshot_id: int, horizon_minutes: int,
                                config_version: str = "v1.0-demo") -> dict[str, Any]:
        """Run Tool 17 against authoritative SolarGrid state/forecast data only."""
        return self._call("analyze_dynamic_pricing", {
            "snapshot_id": snapshot_id,
            "horizon_minutes": horizon_minutes,
            "config_version": config_version,
        })

    def build_and_validate_dynamic_pricing_plans(
        self, *, snapshot_id: int, horizon_minutes: int,
        risk_event_id: int | None = None, reserve_assessment_id: int | None = None,
        objectives: list[str] | None = None, decision_policy_version: str = "v1.0-demo",
        dynamic_pricing_config_version: str = "v1.0-demo", required_candidate_count: int = 4,
        strategies: list[str] | None = None,
    ) -> dict[str, Any]:
        """Tool 17 -> existing Tools 05-09, without creating a parallel lifecycle.

        No raw Dynamic Pricing engineering values or stale ToolCallLog references
        are accepted from the caller. Every DP planning request refreshes Tool 17
        against the requested authoritative snapshot/horizon first. If Tool 17
        finds no executable conserved shift, planning is intentionally skipped.
        """
        analysis = self.analyze_dynamic_pricing(
            snapshot_id=snapshot_id,
            horizon_minutes=horizon_minutes,
            config_version=dynamic_pricing_config_version,
        )
        tool_call_id = _read(analysis, "tool_call_id")
        action_required = _read(analysis, "action_required")
        domain_status = str(_read(analysis, "domain_status", "UNKNOWN") or "UNKNOWN").upper()
        if action_required is not True or tool_call_id is None or domain_status not in {"OK", "SUCCESS"}:
            return {
                "mode": "DYNAMIC_PRICING",
                "dynamic_pricing": analysis,
                "planning": None,
                "selected_plan_id": None,
                "status": "NO_ACTION" if action_required is False else "ANALYSIS_INCOMPLETE",
            }

        planning = self.build_and_validate_plans(
            snapshot_id=snapshot_id,
            horizon_minutes=horizon_minutes,
            risk_event_id=risk_event_id,
            reserve_assessment_id=reserve_assessment_id,
            objectives=objectives,
            decision_policy_version=decision_policy_version,
            required_candidate_count=required_candidate_count,
            strategies=strategies,
            dynamic_pricing_tool_call_id=int(tool_call_id),
        )
        return {
            "mode": "DYNAMIC_PRICING",
            "dynamic_pricing": analysis,
            "dynamic_pricing_tool_call_id": int(tool_call_id),
            "planning": planning,
            "selected_plan_id": planning.get("selected_plan_id"),
            "status": "PLANS_EVALUATED",
        }

    def build_and_validate_plans(self, *, snapshot_id: int, horizon_minutes: int,
                                 risk_event_id: int | None = None,
                                 reserve_assessment_id: int | None = None,
                                 objectives: list[str] | None = None,
                                 decision_policy_version: str = "v1.0-demo",
                                 required_candidate_count: int = 4,
                                 strategies: list[str] | None = None,
                                 dynamic_pricing_tool_call_id: int | None = None) -> dict[str, Any]:
        context = self.resolve_validation_context(
            snapshot_id=snapshot_id,
            horizon_minutes=horizon_minutes,
            decision_policy_version=decision_policy_version,
            reserve_assessment_id=reserve_assessment_id,
            ensure_reserve=True,
        )
        reserve_assessment_id = context["reserve_assessment_id"]
        network_id = context["network_id"]

        payload = {"snapshot_id": snapshot_id, "risk_event_id": risk_event_id,
                   "reserve_assessment_id": reserve_assessment_id,
                   "horizon_minutes": horizon_minutes, "objectives": objectives or [],
                   "decision_policy_version": decision_policy_version,
                   "required_candidate_count": required_candidate_count}
        if strategies is not None:
            payload["strategies"] = strategies
        if dynamic_pricing_tool_call_id is not None:
            payload["dynamic_pricing_context"] = {"tool_call_id": int(dynamic_pricing_tool_call_id)}
        generated = self._call("generate_and_optimize_plans", payload)
        plan_ids = _extract_plan_ids(generated)
        if not plan_ids:
            return {"generated": generated, "evaluated": None, "selected_plan_id": None}
        generator_checks, battery_checks, network_checks = [], [], []
        for plan_id in plan_ids:
            generator_checks.append(self._call("check_generator_constraints", {"plan_id": plan_id, "context_snapshot_id": snapshot_id}))
            battery_checks.append(self._call("check_battery_constraints", {"plan_id": plan_id, "context_snapshot_id": snapshot_id}))
            network_checks.append(self._call("run_power_flow", {
                "plan_id": plan_id,
                "network_id": network_id,
                "context_snapshot_id": snapshot_id,
            }))
        evaluated = self._call("evaluate_plans", {
            "plan_ids": plan_ids,
            "decision_policy_version": decision_policy_version,
            "reserve_assessment_id": reserve_assessment_id,
            "context_snapshot_id": snapshot_id,
            "network_id": network_id,
            "horizon_minutes": horizon_minutes,
        })
        return {"generated": generated, "generator_checks": generator_checks, "battery_checks": battery_checks,
                "network_checks": network_checks, "evaluated": evaluated,
                "selected_plan_id": _read(evaluated, "selected_plan_id")}

    def monitor_active_plan(self, *, plan_id: int, current_snapshot_id: int) -> dict[str, Any]:
        return {"monitoring": self._call("monitor_system_conditions", {"active_plan_id": plan_id, "current_snapshot_id": current_snapshot_id})}

    def replan_after_execution(self, *, plan_id: int, required_candidate_count: int = 4,
                               decision_policy_version: str = "v1.0-demo") -> dict[str, Any]:
        """Generate a replacement candidate set from the latest post-execution state.

        This is deliberately separate from Tool 12 change-driven revalidation. It
        only operates when a persisted execution has a completed OperationalOutcome
        whose goal result requires recovery replanning. The normal Tools 05-09
        pipeline remains authoritative for generation/validation; this method only
        supplies the recovery context and persists lineage.
        """
        if self._replans_used >= self.limits.max_replans:
            raise RuntimeError(f"Planner replan limit exceeded: {self.limits.max_replans}")

        original = self.context.db.session.get(Plan, int(plan_id))
        if original is None:
            raise LookupError(f"Plan with id={plan_id} does not exist")

        from database import PlanExecution
        execution = self.context.db.session.get(PlanExecution, original.execution.id) if original.execution else None
        if execution is None:
            raise ValueError("No execution/outcome exists for this plan; post-execution replanning is not eligible")

        outcome = execution.outcome
        if outcome is None:
            raise ValueError("No operational outcome exists for this execution; post-execution replanning is not eligible")

        try:
            actual_outcome = json.loads(outcome.actual_outcome or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError("The stored operational outcome is invalid; replanning is not eligible") from exc

        if not isinstance(actual_outcome, dict):
            raise ValueError("The stored operational outcome is invalid; replanning is not eligible")

        goal_result = str(actual_outcome.get("goal_result") or "").upper()
        replan_required = bool(actual_outcome.get("replan_required")) or (
            goal_result in {"NOT_ACHIEVED", "PARTIAL"} or execution.status in {"FAILED", "PARTIAL_FAIL"}
        )
        if not replan_required:
            raise ValueError("Post-execution outcome does not require replanning")

        # Repeated operator clicks must reuse an active/pending replacement rather
        # than creating an uncontrolled chain of duplicate candidates.
        active_children = (
            Plan.query
            .filter(Plan.parent_plan_id == original.id)
            .filter(Plan.status.in_({"PROPOSED", "APPROVED"}))
            .order_by(Plan.id.desc())
            .all()
        )
        if active_children:
            return {
                "status": "REPLAN_ALREADY_EXISTS",
                "original_plan_id": original.id,
                "current_snapshot_id": active_children[0].snapshot_id,
                "parent_plan_id": original.id,
                "reused_plan_ids": [item.id for item in active_children],
                "plans": [_plan_summary(item) for item in active_children],
                "selected_plan_id": None,
            }

        latest_snapshot = (
            SystemSnapshot.query
            .order_by(SystemSnapshot.timestamp.desc(), SystemSnapshot.id.desc())
            .first()
        )
        if latest_snapshot is None:
            raise LookupError("No current operational snapshot is available for replanning")

        horizon_minutes = None
        if isinstance(original.actions, dict):
            try:
                dispatch = PlanDispatch.model_validate(original.actions)
                horizon_minutes = int(dispatch.interval_minutes) * len(dispatch.intervals)
            except Exception:
                horizon_minutes = None
        if horizon_minutes is None:
            horizon_minutes = int(round(float(original.horizon_hours or 0) * 60))
        if horizon_minutes <= 0:
            raise ValueError("Original plan does not contain a valid planning horizon")

        generated = self.build_and_validate_plans(
            snapshot_id=int(latest_snapshot.id),
            horizon_minutes=horizon_minutes,
            risk_event_id=original.imbalance_event_id,
            decision_policy_version=decision_policy_version,
            required_candidate_count=required_candidate_count,
        )

        plan_ids = _extract_plan_ids(generated.get("generated"))
        replacement_plans = [self.context.db.session.get(Plan, int(pid)) for pid in plan_ids]
        replacement_plans = [item for item in replacement_plans if item is not None]
        if not replacement_plans:
            return {
                **generated,
                "status": "REPLAN_GENERATED_NO_PLANS",
                "original_plan_id": original.id,
                "current_snapshot_id": latest_snapshot.id,
                "parent_plan_id": original.id,
                "plans": [],
            }

        self._replans_used += 1
        lineage_note = (
            f"Post-execution recovery replacement for Plan #{original.id}; "
            f"trigger={goal_result or execution.status}; source_execution_id={execution.id}; "
            f"source_outcome_id={outcome.id}; source_snapshot_id={actual_outcome.get('snapshot_id')}; "
            f"replanning_snapshot_id={latest_snapshot.id}."
        )
        try:
            for replacement in replacement_plans:
                replacement.parent_plan_id = original.id
                existing = replacement.assumptions or ""
                replacement.assumptions = f"{existing}; {lineage_note}" if existing else lineage_note
            self.context.db.session.commit()
        except Exception:
            self.context.db.session.rollback()
            # Tool 05 commits its candidate batch before returning. If the lineage
            # persistence fails, remove only the candidates created by this call so
            # no unlinked replacement can remain visible.
            for replacement in replacement_plans:
                persisted = self.context.db.session.get(Plan, replacement.id)
                if persisted is not None and persisted.parent_plan_id is None:
                    self.context.db.session.delete(persisted)
            self.context.db.session.commit()
            raise

        return {
            **generated,
            "status": "REPLAN_GENERATED",
            "original_plan_id": original.id,
            "current_snapshot_id": latest_snapshot.id,
            "parent_plan_id": original.id,
            "source_execution_id": execution.id,
            "source_outcome_id": outcome.id,
            "goal_result": goal_result,
            "plans": [_plan_summary(item) for item in replacement_plans],
        }

    def revalidate_active_plan(self, *, plan_id: int, current_snapshot_id: int,
                               monitoring_log_id: int | None = None) -> dict[str, Any]:
        impact = self._call("assess_change_impact", {"plan_id": plan_id, "current_snapshot_id": current_snapshot_id, "monitoring_log_id": monitoring_log_id})
        replan = bool(_read(impact, "replan_required"))
        if replan:
            if self._replans_used >= self.limits.max_replans:
                raise RuntimeError(f"Planner replan limit exceeded: {self.limits.max_replans}")
            self._replans_used += 1
            plan = self.context.db.session.get(Plan, plan_id)
            if plan is not None and plan.status == "APPROVED":
                transition_plan(plan, "STALE")
                self.context.db.session.commit()
        return {"impact": impact, "replan_required": replan}
#new
    def execute_after_human_approval(
            self,
            *,
            plan_id: int,
            approval_id: int,
            current_snapshot_id: int,
        ) -> dict[str, Any]:
            """Run the production pre-execution safety gate, then execute only if allowed.

            Periodic monitoring/revalidation remains exposed through
            ``monitor_active_plan`` / ``revalidate_active_plan``. This method must
            not perform a second standalone Tool 12 call before SafetyAgent because
            SafetyAgent itself performs the authoritative pre-execution change-impact
            refresh against the explicit current snapshot.
            """
            safety = SafetyAgent(
                assess_change_impact_fn=lambda **payload: self._call(
                    "assess_change_impact",
                    payload,
                ),
                retrieve_evidence_fn=lambda **payload: self._call(
                    "retrieve_engineering_evidence",
                    payload,
                ),
            )

            safety_result = safety.validate_plan(
                plan_id,
                current_snapshot_id=current_snapshot_id,
            )

            if safety_result.decision is not SafetyDecision.ALLOW:
                return {
                    "status": "BLOCKED_BY_SAFETY",
                    "executable": False,
                    "execution_eligibility": "BLOCKED_BY_SAFETY",
                    "safety": safety_result.to_dict(),
                }

            execution = self._call(
                "execute_and_verify_plan",
                {
                    "plan_id": plan_id,
                    "approval_id": approval_id,
                    "execution_mode": "SIMULATION",
                },
            )

            execution_id = _read(execution, "execution_id")
            execution_status = str(_read(execution, "execution_status", "UNKNOWN") or "UNKNOWN").upper()

            if execution_id is None:
                return {
                    "status": "EXECUTION_ATTEMPTED",
                    "safety": safety_result.to_dict(),
                    "execution": execution,
                    "post_execution": {
                        "status": "NOT_STARTED",
                        "diagnosis": None,
                        "forecast_analysis": {
                            "status": "NOT_RUN",
                            "reason": "Tool 13 did not return an execution_id.",
                        },
                        "memory": {
                            "status": "NOT_FINALIZED",
                            "reason": "No execution record is available for post-execution memory.",
                        },
                    },
                }

            post_execution_snapshot_id = _read(execution, "post_execution_snapshot_id")
            pre_execution_snapshot_id = _read(execution, "pre_execution_snapshot_id")
            post_execution = self.complete_post_execution_chain(
                execution_id=int(execution_id),
                pre_execution_snapshot_id=(
                    int(pre_execution_snapshot_id)
                    if pre_execution_snapshot_id is not None
                    else None
                ),
                post_execution_snapshot_id=(
                    int(post_execution_snapshot_id)
                    if post_execution_snapshot_id is not None
                    else None
                ),
            )

            return {
                "status": (
                    "VERIFICATION_PENDING"
                    if post_execution.get("status") == "PENDING"
                    else "EXECUTION_COMPLETED"
                    if execution_status == "SUCCESS"
                    else "EXECUTION_ATTEMPTED"
                ),
                "safety": safety_result.to_dict(),
                "execution": execution,
                "post_execution": post_execution,
            }
#--
    def complete_post_execution_chain(
            self,
            *,
            execution_id: int,
            pre_execution_snapshot_id: int | None = None,
            post_execution_snapshot_id: int | None = None,
            forecast_analysis_payload: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Continue Tool 13 -> Tool 14 -> Tool 15 (when explicitly scoped) -> memory.

            Tool 14 owns delayed goal verification. If verification is not due,
            the chain returns PENDING and does not fabricate a diagnosis. Tool 15
            is executed only when the caller supplies an explicit analysis scope;
            this avoids silently analysing unrelated forecasts. Historical case
            memory is backed by the persisted OperationalOutcome created by Tool 14.
            """
            diagnosis = self._call(
                "assess_and_diagnose_outcome",
                {
                    "execution_id": execution_id,
                    "pre_execution_snapshot_id": pre_execution_snapshot_id,
                    "post_execution_snapshot_id": post_execution_snapshot_id,
                },
            )

            diagnosis_status = str(_read(diagnosis, "status", "UNKNOWN") or "UNKNOWN").upper()
            verification_status = str(_read(diagnosis, "verification_status", "") or "").upper()

            if diagnosis_status == "PENDING" or verification_status == "NOT_DUE":
                return {
                    "status": "PENDING",
                    "diagnosis": diagnosis,
                    "forecast_analysis": {
                        "status": "NOT_RUN",
                        "reason": "Tool 14 delayed verification is not due yet.",
                    },
                    "memory": {
                        "status": "PENDING",
                        "reason": "OperationalOutcome has not been finalized yet.",
                    },
                }

            if diagnosis_status != "SUCCESS":
                return {
                    "status": "DIAGNOSIS_INCOMPLETE",
                    "diagnosis": diagnosis,
                    "forecast_analysis": {
                        "status": "NOT_RUN",
                        "reason": "Tool 14 did not complete successfully.",
                    },
                    "memory": {
                        "status": "NOT_FINALIZED",
                        "reason": "No completed Tool 14 outcome is available.",
                    },
                    "replan": {
                        "status": "NOT_TRIGGERED",
                        "reason": "Automatic replanning requires a completed Tool 14 diagnosis.",
                    },
                }

            # Tool 14 is the authoritative post-execution recovery signal.  When
            # it explicitly requires recovery, reuse the existing replanning
            # operation here so autonomous execution and the manual /replan
            # endpoint share the exact same duplicate protection, current-state
            # selection, Tools 05-09 validation, and lineage behavior.
            replan_required = bool(_read(diagnosis, "replan_required"))
            replan_result: dict[str, Any]
            if replan_required:
                from database import PlanExecution
                execution_row = self.context.db.session.get(PlanExecution, int(execution_id))
                recovery_plan_id = execution_row.plan_id if execution_row is not None else None
                if recovery_plan_id is None:
                    replan_result = {
                        "status": "REPLAN_FAILED",
                        "reason": "Tool 14 requested recovery but the execution has no associated plan.",
                    }
                else:
                    try:
                        replan_result = self.replan_after_execution(
                            plan_id=int(recovery_plan_id),
                            required_candidate_count=4,
                        )
                    except Exception as exc:
                        # Diagnosis and memory remain authoritative even if the
                        # downstream recovery planning operation fails.
                        replan_result = {
                            "status": "REPLAN_FAILED",
                            "reason": str(exc),
                            "plan_id": int(recovery_plan_id),
                        }
            else:
                replan_result = {
                    "status": "NOT_REQUIRED",
                    "reason": "Tool 14 did not require post-execution recovery replanning.",
                }

            forecast_analysis: Any = {
                "status": "NOT_TRIGGERED",
                "reason": (
                    "No explicit forecast-analysis scope was supplied. Tool 15 is only "
                    "run when applicable inputs are provided, so unrelated forecasts are not analysed."
                ),
            }
            if forecast_analysis_payload:
                forecast_analysis = self._call(
                    "forecast_error_analysis",
                    dict(forecast_analysis_payload),
                )

            outcome_id = _read(diagnosis, "outcome_id")
            lesson_decision = {
                "status": "NOT_EVALUATED",
                "reason": "OperationalOutcome id is unavailable.",
                "lesson_id": None,
            }
            if outcome_id is not None:
                lesson_decision = SystemMemoryManager().evaluate_outcome_for_reusable_lesson(int(outcome_id))

            forecast_lesson_ids = _read(forecast_analysis, "lessons_created", []) if isinstance(forecast_analysis, dict) else []
            lesson_ids = list(forecast_lesson_ids or [])
            if lesson_decision.get("lesson_id") is not None:
                lesson_ids.append(int(lesson_decision["lesson_id"]))
            memory = {
                "status": "EVALUATED" if outcome_id is not None else "OUTCOME_ID_MISSING",
                "outcome_id": outcome_id,
                "historical_case_available": outcome_id is not None,
                "lesson_decision": lesson_decision,
                "lesson_memory_ids": list(dict.fromkeys(lesson_ids)),
                "note": (
                    "OperationalOutcome is always retained as history. Reusable lessons are created or "
                    "reinforced only by deterministic post-execution eligibility rules."
                ),
            }

            return {
                "status": "COMPLETED",
                "diagnosis": diagnosis,
                "forecast_analysis": forecast_analysis,
                "memory": memory,
                "replan": replan_result,
            }

    def diagnose_execution(
            self,
            *,
            execution_id: int,
            pre_execution_snapshot_id: int | None = None,
            post_execution_snapshot_id: int | None = None,
            forecast_analysis_payload: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            return self.complete_post_execution_chain(
                execution_id=execution_id,
                pre_execution_snapshot_id=pre_execution_snapshot_id,
                post_execution_snapshot_id=post_execution_snapshot_id,
                forecast_analysis_payload=forecast_analysis_payload,
            )

def _plan_summary(plan: Plan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "plan_name": plan.plan_name,
        "status": plan.status,
        "snapshot_id": plan.snapshot_id,
        "parent_plan_id": plan.parent_plan_id,
        "strategy_label": plan.strategy_label,
        "validation_status": (
            "FEASIBLE"
            if plan.evaluations and plan.evaluations[-1].is_feasible is True
            else "INFEASIBLE"
            if plan.evaluations and plan.evaluations[-1].is_feasible is False
            else "UNKNOWN"
        ),
    }


def _read(value: Any, key: str, default=None):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        data = value.get("data")
        if isinstance(data, dict) and key in data:
            return data[key]
        return default
    direct = getattr(value, key, None)
    return default if direct is None else direct


def _extract_snapshot_id(value: Any) -> int | None:
    direct = _read(value, "snapshot_id")
    if direct is not None:
        return int(direct)
    data = _read(value, "data")
    if isinstance(data, dict) and data.get("snapshot_id") is not None:
        return int(data["snapshot_id"])
    return None


def _extract_plan_ids(value: Any) -> list[int]:
    plans = _read(value, "plans", [])
    if not plans:
        data = _read(value, "data")
        plans = data.get("plans", []) if isinstance(data, dict) else []
    return [int(pid) for plan in plans or [] if (pid := _read(plan, "plan_id")) is not None]
