"""
memory.py

Short-Term / Episodic Execution-Trace Memory, Long-Term Historical Case
Memory, and Long-Term Lesson Memory management for the agent system.

This module does NOT define, modify, or extend any database models. It
exclusively imports and operates on the models already defined in
`database.py`.

Memory responsibilities are separated into three concerns:

    * Current execution trace: what happened during the current run.
    * Historical similar cases: whether a similar operating situation was
      observed in previous snapshots/runs and what was recorded.
    * Long-term lessons: what lessons were extracted from previous failures
      and forecast errors.

All persistence goes through the existing Flask-SQLAlchemy `db` / `db.session`
objects defined in `database.py`. No second SQLAlchemy instance, no raw SQL,
no vector databases, embeddings, external AI calls, or additional
persistence layers are used. Similarity and condition matching are fully
deterministic and computed locally.

This module is a persistence/retrieval layer only. It records and retrieves
facts; it never decides which operational action to take next. That reasoning
belongs to the Planner / Safety Agent components.
"""

import difflib
import json
import uuid
from numbers import Real
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from database import (
    db,
    AgentRunTrace,
    ToolCallLog,
    SystemSnapshot,
    ImbalanceEvent,
    Plan,
    PlanExecution,
    OperationalOutcome,
    LessonMemory,
    ForecastErrorLog,
)


# Minimum SequenceMatcher ratio at which two observed patterns are treated
# as referring to the same underlying lesson.
_SIMILARITY_THRESHOLD = 0.85

# Minimum confidence score a LessonMemory must have to be considered
# "active" for planning purposes.
_MIN_ACTIVE_CONFIDENCE = 0.3

# Amount by which confidence_score is increased each time a matching
# lesson recurs, capped at 1.0.
_CONFIDENCE_INCREMENT = 0.10

# Maximum number of historical cases exposed to the Planner at once.
_MAX_HISTORICAL_CASES = 5

# Snapshot fields used for deterministic historical-case comparison.
_SNAPSHOT_NUMERIC_FIELDS = (
    "demand_mw",
    "solar_gen_mw",
    "other_gen_mw",
    "battery_soc_pct",
    "reserve_margin_mw",
)


class SystemMemoryManager:
    """
    Provides three separate memory responsibilities:

    1. Current execution trace memory: start/close an `AgentRunTrace`, log
       `ToolCallLog` rows, and summarize the current run's tool-call history.
    2. Historical case memory: retrieve up to five deterministic, factual
       historical cases similar to a current `SystemSnapshot`.
    3. Long-term lesson memory: create/reinforce and retrieve active
       `LessonMemory` records.

    This class does not define any database models. It reads and writes
    exclusively through the SQLAlchemy models already declared in
    `database.py`. It never makes operational decisions; the Planner and
    Safety Agent remain responsible for interpretation and action selection.
    """

    # ------------------------------------------------------------------
    # 1. SHORT-TERM / EXECUTION TRACE MEMORY
    # ------------------------------------------------------------------
    def create_agent_run_trace(self, trigger_source: str) -> AgentRunTrace:
        """
        Start a new agent run by creating and persisting an `AgentRunTrace`.

        Args:
            trigger_source: What triggered this run (e.g. 'SCHEDULED',
                'EVENT_IMBALANCE', 'USER_PROMPT').

        Returns:
            The newly created `AgentRunTrace`.
        """
        try:
            run_trace = AgentRunTrace(
                run_uuid=str(uuid.uuid4()),
                trigger_source=trigger_source,
            )
            db.session.add(run_trace)
            db.session.commit()
            return run_trace
        except Exception:
            db.session.rollback()
            raise

    def log_tool_execution(
        self,
        run_id: int,
        step_index: int,
        tool_name: str,
        inputs: Any,
        outputs: Any,
        status: str,
    ) -> ToolCallLog:
        """
        Record a single tool invocation for an existing agent run.

        Failed calls are recorded just like successful calls so the current
        run's factual history remains complete.
        """
        run_trace = db.session.get(AgentRunTrace, run_id)
        if run_trace is None:
            raise LookupError(f"AgentRunTrace with id={run_id} does not exist.")

        try:
            tool_call = ToolCallLog(
                run_id=run_id,
                step_index=step_index,
                tool_name=tool_name,
                input_json=inputs,
                output_json=outputs,
                status=status,
            )
            db.session.add(tool_call)
            db.session.commit()
            return tool_call
        except Exception:
            db.session.rollback()
            raise

    def get_run_history(
        self,
        run_id: int,
        *,
        include_details: bool = False,
        max_chars: int = 12000,
    ) -> str:
        """Return deterministic current-run history in summary or detail mode.

        ``include_details=False`` preserves the original compact API.  Detailed
        mode exposes the persisted tool inputs/outputs so the Planner can see
        intermediate facts from the current run without querying the database
        directly.  Payloads are normalized and bounded to keep LLM context
        usable; historical memory is intentionally not mixed into this trace.
        """
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")

        tool_calls = (
            ToolCallLog.query
            .filter(ToolCallLog.run_id == run_id)
            .order_by(ToolCallLog.step_index.asc(), ToolCallLog.id.asc())
            .all()
        )

        if not tool_calls:
            return f"No tool calls were recorded for run {run_id}."

        if not include_details:
            return "\n".join(
                f"Step {call.step_index}: Called {call.tool_name} - Status: {call.status}"
                for call in tool_calls
            )

        blocks = []
        for call in tool_calls:
            block = [
                f"Step {call.step_index}",
                f"Tool: {call.tool_name}",
                f"Category: {self._safe_text(call.tool_category)}",
                f"Status: {call.status}",
                f"Input: {self._format_trace_payload(call.input_json)}",
                f"Output: {self._format_trace_payload(call.output_json)}",
            ]
            if call.error_message:
                block.append(f"Error: {self._safe_text(call.error_message)}")
            if call.latency_ms is not None:
                block.append(f"Latency_ms: {call.latency_ms}")
            blocks.append("\n".join(block))

        header = f"Detailed Current Run History (run_id={run_id}):"
        text = header + "\n\n" + "\n\n".join(blocks)
        if len(text) <= max_chars:
            return text

        # Deterministically retain complete earliest steps first, then append a
        # clear truncation marker. Never silently cut JSON in the middle.
        retained = [header]
        used = len(header)
        for block in blocks:
            addition = "\n\n" + block
            if used + len(addition) + 40 > max_chars:
                break
            retained.append(addition)
            used += len(addition)
        retained.append("\n\n[Detailed run history truncated to the configured context limit.]")
        return "".join(retained)

    @staticmethod
    def _format_trace_payload(value: Any, max_value_chars: int = 3000) -> str:
        """Serialize a trace payload safely and deterministically."""
        if value is None:
            return "NULL"
        try:
            text = json.dumps(value, sort_keys=True, default=str, ensure_ascii=True)
        except (TypeError, ValueError):
            text = str(value)
        if len(text) <= max_value_chars:
            return text
        return text[:max_value_chars] + "... [payload truncated]"

    def close_agent_run_trace(
        self,
        run_id: int,
        final_status: str,
        summary: str,
    ) -> AgentRunTrace:
        """
        Mark an existing agent run as finished without altering its tool-call
        history.
        """
        run_trace = db.session.get(AgentRunTrace, run_id)
        if run_trace is None:
            raise LookupError(f"AgentRunTrace with id={run_id} does not exist.")

        try:
            run_trace.status = final_status
            run_trace.final_outcome_summary = summary
            run_trace.end_time = datetime.utcnow()
            db.session.commit()
            return run_trace
        except Exception:
            db.session.rollback()
            raise

    # ------------------------------------------------------------------
    # 2. LONG-TERM / HISTORICAL CASE MEMORY
    # ------------------------------------------------------------------
    def search_similar_cases(
        self,
        snapshot_id: int,
        query_type: str,
    ) -> str:
        """
        Retrieve a small deterministic set of historical operational cases
        similar to the supplied current snapshot and format them as plain
        text for direct injection into the Planner's LLM context.

        Supported query types are:
            * ``"imbalance_event"`` — historical imbalance-event cases only.
            * ``"operational_outcome"`` — historical outcome cases only.
            * ``"both"`` — both case types, merged by historical snapshot so
              the same underlying operational situation is not duplicated.

        Similarity uses only fields that actually exist in `database.py`:
        snapshot operating values/status plus, when available, imbalance-event
        type/severity/expected imbalance. Numeric similarity is based on a
        transparent relative-difference calculation; categorical fields use
        exact equality. Missing values are ignored rather than invented.

        The current snapshot itself and snapshots dated at/after it are not
        treated as historical cases. No LLM, embeddings, external API, or
        operational decision logic is used.

        Raises:
            LookupError: If `snapshot_id` does not identify a `SystemSnapshot`.
            ValueError: If `query_type` is unsupported.
        """
        normalized_query_type = query_type.strip().lower()
        supported_types = {
            "imbalance_event",
            "operational_outcome",
            "both",
        }
        if normalized_query_type not in supported_types:
            raise ValueError(
                "Unsupported query_type={!r}. Expected one of: "
                "'imbalance_event', 'operational_outcome', or 'both'.".format(
                    query_type
                )
            )

        current_snapshot = db.session.get(SystemSnapshot, snapshot_id)
        if current_snapshot is None:
            raise LookupError(
                f"SystemSnapshot with id={snapshot_id} does not exist."
            )

        # Keep the reference case separate from history. A strict timestamp
        # check prevents future snapshots from being exposed as "past" cases.
        historical_snapshots_query = SystemSnapshot.query.filter(
            SystemSnapshot.id != current_snapshot.id,
            SystemSnapshot.timestamp < current_snapshot.timestamp,
        )
        historical_snapshots = historical_snapshots_query.order_by(
            SystemSnapshot.timestamp.asc(),
            SystemSnapshot.id.asc(),
        ).all()

        if not historical_snapshots:
            return (
                "No similar historical cases were found for "
                f"snapshot {snapshot_id} and query_type "
                f"'{normalized_query_type}'."
            )

        historical_snapshot_ids = [snapshot.id for snapshot in historical_snapshots]

        historical_events: Sequence[ImbalanceEvent] = ()
        historical_outcomes: Sequence[OperationalOutcome] = ()

        if normalized_query_type in {"imbalance_event", "both"}:
            historical_events = (
                ImbalanceEvent.query
                .filter(ImbalanceEvent.snapshot_id.in_(historical_snapshot_ids))
                .order_by(ImbalanceEvent.detection_time.asc(), ImbalanceEvent.id.asc())
                .all()
            )

        if normalized_query_type in {"operational_outcome", "both"}:
            historical_outcomes = (
                OperationalOutcome.query
                .join(PlanExecution, OperationalOutcome.execution_id == PlanExecution.id)
                .join(Plan, PlanExecution.plan_id == Plan.id)
                .filter(Plan.snapshot_id.in_(historical_snapshot_ids))
                .order_by(
                    PlanExecution.execution_timestamp.asc(),
                    OperationalOutcome.id.asc(),
                )
                .all()
            )

        events_by_snapshot: Dict[int, List[ImbalanceEvent]] = {}
        for event in historical_events:
            events_by_snapshot.setdefault(event.snapshot_id, []).append(event)

        outcomes_by_snapshot: Dict[int, List[OperationalOutcome]] = {}
        for outcome in historical_outcomes:
            outcome_snapshot_id = outcome.execution.plan.snapshot_id
            outcomes_by_snapshot.setdefault(outcome_snapshot_id, []).append(outcome)

        current_events = list(current_snapshot.imbalance_events or [])

        candidates: List[Tuple[float, datetime, int, Dict[str, Any]]] = []
        for historical_snapshot in historical_snapshots:
            events = events_by_snapshot.get(historical_snapshot.id, [])
            outcomes = outcomes_by_snapshot.get(historical_snapshot.id, [])

            if normalized_query_type == "imbalance_event" and not events:
                continue
            if normalized_query_type == "operational_outcome" and not outcomes:
                continue
            if normalized_query_type == "both" and not events and not outcomes:
                continue

            current_event, historical_event = self._best_matching_event_pair(
                current_events, events
            )
            score, matched_fields = self._historical_case_similarity(
                current_snapshot=current_snapshot,
                historical_snapshot=historical_snapshot,
                current_event=current_event,
                historical_event=historical_event,
            )

            candidates.append(
                (
                    score,
                    historical_snapshot.timestamp,
                    historical_snapshot.id,
                    {
                        "snapshot": historical_snapshot,
                        "events": events,
                        "outcomes": outcomes,
                        "matched_fields": matched_fields,
                    },
                )
            )

        if not candidates:
            return (
                "No similar historical cases were found for "
                f"snapshot {snapshot_id} and query_type "
                f"'{normalized_query_type}'."
            )

        # Highest similarity first. Timestamp and id are stable tie-breakers,
        # so identical database contents always produce identical ordering.
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        selected = candidates[:_MAX_HISTORICAL_CASES]

        blocks = ["Similar Historical Cases:"]
        for index, (score, _, _, case) in enumerate(selected, start=1):
            blocks.append(
                self._format_historical_case(
                    case_number=index,
                    case=case,
                    similarity_score=score,
                    query_type=normalized_query_type,
                )
            )

        return "\n\n".join(blocks)

    def _historical_case_similarity(
        self,
        current_snapshot: SystemSnapshot,
        historical_snapshot: SystemSnapshot,
        current_event: Optional[ImbalanceEvent],
        historical_event: Optional[ImbalanceEvent],
    ) -> Tuple[float, List[str]]:
        """
        Compare a historical snapshot to the current snapshot using only
        schema-backed operational fields. Every available feature contributes
        equally to the arithmetic mean; unavailable features are omitted.
        """
        feature_scores: List[Tuple[str, float]] = []

        for field_name in _SNAPSHOT_NUMERIC_FIELDS:
            current_value = getattr(current_snapshot, field_name)
            historical_value = getattr(historical_snapshot, field_name)
            if current_value is None or historical_value is None:
                continue
            feature_scores.append(
                (
                    field_name,
                    self._numeric_similarity(current_value, historical_value),
                )
            )

        if current_snapshot.grid_status is not None and historical_snapshot.grid_status is not None:
            feature_scores.append(
                (
                    "grid_status",
                    1.0
                    if current_snapshot.grid_status == historical_snapshot.grid_status
                    else 0.0,
                )
            )

        if current_event is not None and historical_event is not None:
            if current_event.event_type is not None and historical_event.event_type is not None:
                feature_scores.append(
                    (
                        "event_type",
                        1.0
                        if current_event.event_type == historical_event.event_type
                        else 0.0,
                    )
                )

            if current_event.severity is not None and historical_event.severity is not None:
                feature_scores.append(
                    (
                        "event_severity",
                        1.0
                        if current_event.severity == historical_event.severity
                        else 0.0,
                    )
                )

            feature_scores.append(
                (
                    "expected_imbalance_mw",
                    self._numeric_similarity(
                        current_event.expected_imbalance_mw,
                        historical_event.expected_imbalance_mw,
                    ),
                )
            )

        if not feature_scores:
            return 0.0, []

        score = sum(value for _, value in feature_scores) / len(feature_scores)
        # Report only features that actually matched strongly so the prompt
        # explains why the case was surfaced without exposing implementation
        # details beyond the deterministic comparison.
        matched_fields = [
            name for name, value in feature_scores if value >= 0.5
        ]
        return score, matched_fields

    @staticmethod
    def _numeric_similarity(current_value: float, historical_value: float) -> float:
        """
        Convert relative numeric difference into a bounded similarity score.

        1.0 means identical. 0.0 means the absolute difference is at least as
        large as the larger absolute value. The calculation is deterministic
        and scale-aware, with ``1.0`` preventing division by zero.
        """
        denominator = max(abs(float(current_value)), abs(float(historical_value)), 1.0)
        relative_difference = abs(
            float(current_value) - float(historical_value)
        ) / denominator
        return max(0.0, 1.0 - relative_difference)

    @staticmethod
    def _best_matching_event_pair(
        current_events: Sequence[ImbalanceEvent],
        historical_events: Sequence[ImbalanceEvent],
    ) -> Tuple[Optional[ImbalanceEvent], Optional[ImbalanceEvent]]:
        """
        Select the current/historical event pair with the highest deterministic
        event-only similarity. Ties resolve by historical event id, then current
        event id.
        """
        if not historical_events or not current_events:
            return (None, historical_events[0] if historical_events else None)

        ranked: List[Tuple[float, int, int, ImbalanceEvent, ImbalanceEvent]] = []
        for historical_event in historical_events:
            for current_event in current_events:
                components = [
                    1.0
                    if current_event.event_type == historical_event.event_type
                    else 0.0,
                    1.0
                    if current_event.severity == historical_event.severity
                    else 0.0,
                    SystemMemoryManager._numeric_similarity(
                        current_event.expected_imbalance_mw,
                        historical_event.expected_imbalance_mw,
                    ),
                ]
                event_score = sum(components) / len(components)
                ranked.append(
                    (
                        event_score,
                        historical_event.id,
                        current_event.id,
                        current_event,
                        historical_event,
                    )
                )

        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        best = ranked[0]
        return best[3], best[4]

    def _format_historical_case(
        self,
        case_number: int,
        case: Dict[str, Any],
        similarity_score: float,
        query_type: str,
    ) -> str:
        """Format one historical case as concise prompt-ready factual text."""
        snapshot: SystemSnapshot = case["snapshot"]
        events: Sequence[ImbalanceEvent] = case["events"]
        outcomes: Sequence[OperationalOutcome] = case["outcomes"]
        matched_fields: Sequence[str] = case["matched_fields"]

        if query_type == "imbalance_event":
            type_label = "Imbalance Event"
        elif query_type == "operational_outcome":
            type_label = "Operational Outcome"
        else:
            type_label = "Imbalance Event + Operational Outcome"

        lines = [
            f"Case {case_number}",
            f"Type: {type_label}",
            f"Historical Snapshot ID: {snapshot.id}",
            f"Snapshot Time: {self._safe_text(snapshot.timestamp)}",
            f"Similarity Score: {similarity_score:.2f}",
            "Similarity Context: " + (
                ", ".join(matched_fields) if matched_fields else "No comparable fields were available"
            ),
            (
                "Snapshot Conditions: "
                f"demand_mw={self._safe_text(snapshot.demand_mw)}, "
                f"solar_gen_mw={self._safe_text(snapshot.solar_gen_mw)}, "
                f"other_gen_mw={self._safe_text(snapshot.other_gen_mw)}, "
                f"battery_soc_pct={self._safe_text(snapshot.battery_soc_pct)}, "
                f"reserve_margin_mw={self._safe_text(snapshot.reserve_margin_mw)}, "
                f"grid_status={self._safe_text(snapshot.grid_status)}"
            ),
        ]

        if query_type in {"imbalance_event", "both"} and events:
            for event in events:
                lines.append(
                    "Imbalance Event: "
                    f"id={event.id}; "
                    f"event_type={self._safe_text(event.event_type)}; "
                    f"severity={self._safe_text(event.severity)}; "
                    f"expected_imbalance_mw={self._safe_text(event.expected_imbalance_mw)}; "
                    f"status={self._safe_text(event.status)}"
                )

        if query_type in {"operational_outcome", "both"}:
            for outcome in outcomes:
                execution = outcome.execution
                plan = execution.plan
                goal_result = None
                try:
                    parsed_actual = json.loads(outcome.actual_outcome) if isinstance(outcome.actual_outcome, str) else outcome.actual_outcome
                    if isinstance(parsed_actual, dict):
                        goal_result = parsed_actual.get("goal_result")
                except (TypeError, ValueError, json.JSONDecodeError):
                    goal_result = None

                lines.append(
                    "Operational Outcome: "
                    f"id={outcome.id}; "
                    f"is_success={self._safe_text(outcome.is_success)}; "
                    f"goal_result={self._safe_text(goal_result)}; "
                    f"actual_outcome={self._safe_text(outcome.actual_outcome)}; "
                    f"operational_impact={self._safe_text(outcome.operational_impact)}"
                )
                #new
                post_snapshot = self._post_execution_snapshot(outcome)

                if post_snapshot is not None:
                    lines.append(
                        "Post-Execution State: "
                        f"snapshot_id={post_snapshot.id}; "
                        f"timestamp={self._safe_text(post_snapshot.timestamp)}; "
                        f"demand_mw={self._safe_text(post_snapshot.demand_mw)}; "
                        f"solar_gen_mw={self._safe_text(post_snapshot.solar_gen_mw)}; "
                        f"other_gen_mw={self._safe_text(post_snapshot.other_gen_mw)}; "
                        f"battery_soc_pct={self._safe_text(post_snapshot.battery_soc_pct)}; "
                        f"reserve_margin_mw={self._safe_text(post_snapshot.reserve_margin_mw)}; "
                        f"grid_status={self._safe_text(post_snapshot.grid_status)}"
                    )

                    if (
                        snapshot.battery_soc_pct is not None
                        and post_snapshot.battery_soc_pct is not None
                    ):
                        lines.append(
                            "Battery State Change: "
                            f"before={self._safe_text(snapshot.battery_soc_pct)}%; "
                            f"after={self._safe_text(post_snapshot.battery_soc_pct)}%; "
                            f"delta={self._safe_text(post_snapshot.battery_soc_pct - snapshot.battery_soc_pct)} "
                            "percentage_points"
                        )
            #end new
                lines.append(
                    "Outcome Context: "
                    f"expected_outcome={self._safe_text(outcome.expected_outcome)}; "
                    f"causes={self._safe_text(outcome.causes)}; "
                    f"execution_status={self._safe_text(execution.status)}; "
                    f"plan_name={self._safe_text(plan.plan_name)}; "
                    f"plan_status={self._safe_text(plan.status)}; "
                    f"strategy_label={self._safe_text(plan.strategy_label)}; "
                    f"optimization_status={self._safe_text(plan.optimization_status)}"
                )
                lines.append(
                    "Recorded Planning Implications: "
                    f"{self._safe_text(outcome.planning_implications)}"
                )

        return "\n".join(lines)

#new
    @staticmethod
    def _post_execution_snapshot(
        outcome: OperationalOutcome,
    ) -> Optional[SystemSnapshot]:
        """Resolve the explicit post-execution snapshot recorded by Tool 14."""
        try:
            actual = json.loads(outcome.actual_outcome or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

        snapshot_id = actual.get("snapshot_id") if isinstance(actual, dict) else None

        if snapshot_id is None:
            return None

        return db.session.get(SystemSnapshot, int(snapshot_id))
#new
    
    @staticmethod
    def _safe_text(value: Any) -> str:
        """Render a database value as a compact single-line string."""
        if value is None:
            return "None"
        return " ".join(str(value).split())

    # ------------------------------------------------------------------
    # 3. LONG-TERM / LESSON MEMORY
    # ------------------------------------------------------------------

    def evaluate_outcome_for_reusable_lesson(self, outcome_id: int) -> dict[str, Any]:
        """Deterministically decide whether a finalized outcome should create/reinforce a lesson.

        Failure/partial patterns are eligible immediately. Successful ACHIEVED patterns
        require at least two comparable finalized outcomes so one success remains history only.
        """
        outcome = db.session.get(OperationalOutcome, outcome_id)
        if outcome is None:
            raise LookupError(f"OperationalOutcome with id={outcome_id} does not exist.")
        execution = outcome.execution
        plan = execution.plan if execution else None
        if execution is None or plan is None:
            return {"status": "HISTORY_ONLY", "reason": "Outcome is not linked to a complete execution/plan record.", "lesson_id": None}
        try:
            actual = json.loads(outcome.actual_outcome or "{}")
        except (TypeError, json.JSONDecodeError):
            actual = {}
        goal_result = str(actual.get("goal_result") or "NOT_EVALUATED").upper()
        strategy = str(plan.strategy_label or plan.plan_name or "operational").strip()
        criteria = actual.get("goal_criteria") if isinstance(actual.get("goal_criteria"), dict) else {}
        conditions = {
            "strategy_label": strategy,
            "goal_balance": criteria.get("balance"),
            "goal_reserve": criteria.get("reserve"),
        }

        # Phase 7: Dynamic Pricing deviations are reusable operational lessons
        # even when the grid-level balance/reserve goal was achieved. This keeps
        # "did the grid remain safe?" separate from "did the pricing response
        # behave as forecast?" and avoids losing an evidence-backed response
        # calibration signal inside a generic execution lesson.
        dynamic_pricing = actual.get("dynamic_pricing") if isinstance(actual.get("dynamic_pricing"), dict) else None
        if dynamic_pricing is not None and dynamic_pricing.get("significant_deviation") is True:
            lesson_code = str(dynamic_pricing.get("lesson_code") or "DYNAMIC_PRICING_IMPACT_DEVIATION")
            lesson_signal = str(
                dynamic_pricing.get("lesson_signal")
                or "Review Dynamic Pricing response assumptions before reusing this pattern."
            )
            conditions.update({
                "dynamic_pricing": True,
                "energy_scope": "SOLAR_ONLY",
                "dynamic_pricing_lesson_code": lesson_code,
            })
            expected_shift = dynamic_pricing.get("expected_load_shift_mw")
            actual_shift = dynamic_pricing.get("actual_load_shift_mw")
            expected_surplus = dynamic_pricing.get("expected_surplus_reduction_mw")
            actual_surplus = dynamic_pricing.get("actual_surplus_reduction_mw")
            pattern = f"{strategy}: Dynamic Pricing {lesson_code.lower()}"
            evidence_summary = (
                f"Execution #{execution.id} Dynamic Pricing deviation: "
                f"expected_load_shift_mw={self._safe_text(expected_shift)}, "
                f"actual_load_shift_mw={self._safe_text(actual_shift)}, "
                f"expected_surplus_reduction_mw={self._safe_text(expected_surplus)}, "
                f"actual_surplus_reduction_mw={self._safe_text(actual_surplus)}; "
                f"deviations={self._safe_text(dynamic_pricing.get('deviations'))}."
            )
            lesson = self._record_lesson(
                pattern=pattern,
                conditions=conditions,
                evidence_summary=evidence_summary,
                implication=lesson_signal,
                operational_impact=outcome.operational_impact,
                source_outcome_id=outcome.id,
                source_forecast_error_id=None,
            )
            return {
                "status": "CREATED_OR_REINFORCED",
                "reason": "A material Dynamic Pricing expected-vs-actual deviation is reusable after one evidence-backed occurrence.",
                "lesson_id": lesson.id,
                "lesson_code": lesson_code,
            }

        if goal_result in {"NOT_ACHIEVED", "PARTIAL"} or execution.status in {"FAILED", "PARTIAL_FAIL"}:
            pattern = f"{strategy}: post-execution goal {goal_result.lower()}"
            implication = "Review or replan this dispatch pattern when comparable operating conditions recur."
            lesson = self._record_lesson(
                pattern=pattern,
                conditions=conditions,
                evidence_summary=f"Evidence-backed outcome from execution #{execution.id}: {goal_result}.",
                implication=implication,
                operational_impact=outcome.operational_impact,
                source_outcome_id=outcome.id,
                source_forecast_error_id=None,
            )
            return {"status": "CREATED_OR_REINFORCED", "reason": "Failure/partial outcomes are reusable after one evidence-backed occurrence.", "lesson_id": lesson.id}
        if goal_result != "ACHIEVED":
            return {"status": "HISTORY_ONLY", "reason": "Goal result is not sufficient to establish a reusable lesson.", "lesson_id": None}

        comparable = 0
        for candidate in OperationalOutcome.query.all():
            candidate_execution = candidate.execution
            candidate_plan = candidate_execution.plan if candidate_execution else None
            if candidate_plan is None or str(candidate_plan.strategy_label or candidate_plan.plan_name or "operational").strip() != strategy:
                continue
            try:
                candidate_actual = json.loads(candidate.actual_outcome or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if str(candidate_actual.get("goal_result") or "").upper() == "ACHIEVED":
                comparable += 1
        if comparable < 2:
            return {"status": "HISTORY_ONLY", "reason": "A single successful outcome remains execution history; two comparable ACHIEVED outcomes are required.", "lesson_id": None, "comparable_successes": comparable}
        pattern = f"{strategy}: repeated achieved post-execution goal"
        lesson = self._record_lesson(
            pattern=pattern,
            conditions=conditions,
            evidence_summary=f"Observed in {comparable} comparable ACHIEVED executions; latest execution #{execution.id}.",
            implication="Use this repeated pattern as supporting historical evidence when comparable conditions are present; deterministic validation and Safety still govern execution.",
            operational_impact=outcome.operational_impact,
            source_outcome_id=outcome.id,
            source_forecast_error_id=None,
        )
        if (lesson.frequency_count or 0) < comparable:
            lesson.frequency_count = comparable
            db.session.commit()
        return {"status": "CREATED_OR_REINFORCED", "reason": "Successful pattern met the two-occurrence reuse threshold.", "lesson_id": lesson.id, "comparable_successes": comparable}

    def record_lesson_from_outcome(
        self,
        outcome_id: int,
        pattern: str,
        conditions: dict,
        evidence_summary: str,
        implication: str,
        operational_impact: Optional[str] = None,
    ) -> LessonMemory:
        """
        Record (or reinforce) a long-term lesson derived from a failed
        `OperationalOutcome`.
        """
        outcome = db.session.get(OperationalOutcome, outcome_id)
        if outcome is None:
            raise LookupError(
                f"OperationalOutcome with id={outcome_id} does not exist."
            )

        if outcome.is_success is not False:
            raise ValueError(
                "Lessons may only be recorded from failed operational "
                f"outcomes (is_success is False); outcome {outcome_id} has "
                f"is_success={outcome.is_success!r}."
            )

        return self._record_lesson(
            pattern=pattern,
            conditions=conditions,
            evidence_summary=evidence_summary,
            implication=implication,
            operational_impact=operational_impact,
            source_outcome_id=outcome_id,
            source_forecast_error_id=None,
        )

    def record_lesson_from_forecast_error(
        self,
        error_id: int,
        pattern: str,
        conditions: dict,
        evidence_summary: str,
        implication: str,
        operational_impact: Optional[str] = None,
    ) -> LessonMemory:
        """Record (or reinforce) a lesson derived from a `ForecastErrorLog`."""
        error_log = db.session.get(ForecastErrorLog, error_id)
        if error_log is None:
            raise LookupError(
                f"ForecastErrorLog with id={error_id} does not exist."
            )

        return self._record_lesson(
            pattern=pattern,
            conditions=conditions,
            evidence_summary=evidence_summary,
            implication=implication,
            operational_impact=operational_impact,
            source_outcome_id=None,
            source_forecast_error_id=error_id,
        )

    def get_active_lessons(self, current_conditions: dict) -> str:
        """
        Retrieve active sufficiently-confident lessons whose stored
        conditions are compatible with `current_conditions`.
        """
        now = datetime.utcnow()

        candidate_lessons = (
            LessonMemory.query
            .filter(
                db.or_(
                    LessonMemory.valid_until.is_(None),
                    LessonMemory.valid_until > now,
                ),
                LessonMemory.confidence_score.isnot(None),
                LessonMemory.confidence_score >= _MIN_ACTIVE_CONFIDENCE,
            )
            .all()
        )

        relevant_lessons = [
            lesson
            for lesson in candidate_lessons
            if self._conditions_match(lesson.conditions, current_conditions)
        ]

        if not relevant_lessons:
            return "No relevant active lessons were found for the current conditions."

        blocks = [
            "* Observed Pattern: {pattern}\n"
            "* Evidence: {evidence}\n"
            "* Rule/Implication: {implication}".format(
                pattern=lesson.observed_pattern,
                evidence=lesson.evidence_summary,
                implication=lesson.planning_implication,
            )
            for lesson in relevant_lessons
        ]
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # Private helpers (shared deterministic lesson logic)
    # ------------------------------------------------------------------
    def _record_lesson(
        self,
        pattern: str,
        conditions: dict,
        evidence_summary: str,
        implication: str,
        operational_impact: Optional[str],
        source_outcome_id: Optional[int],
        source_forecast_error_id: Optional[int],
    ) -> LessonMemory:
        """
        Shared implementation for finding-and-reinforcing or creating a
        `LessonMemory`.
        """
        existing_lesson = self._find_similar_lesson(pattern)

        try:
            if existing_lesson is not None:
                current_confidence = existing_lesson.confidence_score
                if current_confidence is None:
                    current_confidence = 0.0
                existing_lesson.frequency_count = (
                    (existing_lesson.frequency_count or 0) + 1
                )
                existing_lesson.confidence_score = min(
                    1.0, current_confidence + _CONFIDENCE_INCREMENT
                )
                existing_lesson.evidence_summary = evidence_summary
                existing_lesson.conditions = conditions
                existing_lesson.operational_impact = operational_impact
                existing_lesson.planning_implication = implication
                if source_outcome_id is not None:
                    existing_lesson.source_outcome_id = source_outcome_id
                if source_forecast_error_id is not None:
                    existing_lesson.source_forecast_error_id = source_forecast_error_id
                db.session.commit()
                return existing_lesson

            new_lesson = LessonMemory(
                source_outcome_id=source_outcome_id,
                source_forecast_error_id=source_forecast_error_id,
                observed_pattern=pattern,
                evidence_summary=evidence_summary,
                frequency_count=1,
                confidence_score=0.5,
                conditions=conditions,
                operational_impact=operational_impact,
                planning_implication=implication,
            )
            db.session.add(new_lesson)
            db.session.commit()
            return new_lesson
        except Exception:
            db.session.rollback()
            raise

    def _find_similar_lesson(self, pattern: str) -> Optional[LessonMemory]:
        """
        Search existing lessons using deterministic exact/fuzzy text matching.
        """
        normalized_new = self._normalize_pattern(pattern)

        for existing_lesson in LessonMemory.query.all():
            normalized_existing = self._normalize_pattern(
                existing_lesson.observed_pattern
            )
            if normalized_existing == normalized_new:
                return existing_lesson

            ratio = difflib.SequenceMatcher(
                None, normalized_existing, normalized_new
            ).ratio()
            if ratio >= _SIMILARITY_THRESHOLD:
                return existing_lesson

        return None

    @staticmethod
    def _normalize_pattern(pattern: str) -> str:
        """Normalize a lesson pattern for deterministic comparison."""
        return " ".join(pattern.strip().lower().split())

    def _conditions_match(
        self,
        lesson_conditions: Optional[Dict[str, Any]],
        current_conditions: Dict[str, Any],
    ) -> bool:
        """
        Determine whether stored lesson conditions are a subset match of the
        current conditions, including nested dictionaries.
        """
        if not lesson_conditions:
            return True

        for key, expected_value in lesson_conditions.items():
            if key not in current_conditions:
                return False

            actual_value = current_conditions[key]

            if isinstance(expected_value, dict):
                if not isinstance(actual_value, dict):
                    return False
                if not self._conditions_match(expected_value, actual_value):
                    return False
            else:
                if isinstance(expected_value, Real) and isinstance(actual_value, Real):
                    similarity = self._numeric_similarity(
                        actual_value,
                        expected_value,
                    )

                    if similarity <= 0.5:
                        return False

                elif actual_value != expected_value:
                    return False
        return True
