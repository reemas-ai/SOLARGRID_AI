"""
SOLARGRID AI - Safety Agent (validation gate).

    Plan -> [Human Approval] -> [Lifecycle] -> [Change Impact / Tool 12]
         -> [Engineering evidence / Tool 16] -> [Deterministic feasibility]
         -> ALLOW | BLOCK

The Safety Agent VALIDATES. It never executes (Tool 13), never plans or
replans (Planner), never changes plan status, and writes nothing itself.

Responsibilities and their sources of truth
-------------------------------------------
* Human Approval       -> HumanApproval row for the exact plan_id, status APPROVED.
                          LLM text / recommendations are never consulted.
* Plan lifecycle       -> Plan.status (existing PlanStatus). Must be APPROVED.
* Change impact        -> Tool 12 (assess_change_impact) / ChangeImpactAssessment.
                          replan_required=True => BLOCK; the Planner replans.
* Engineering evidence -> Tool 16 (retrieve_engineering_evidence) ONLY. There is
                          no second retrieval path in this module.
* Operational feasibility -> the latest persisted PlanEvaluation (Tool 09 output).
                          Nothing is recomputed here.

Evidence semantics (important)
------------------------------
Tool 16 is retrieval-only, so the ENGINEERING_EVIDENCE check proves that
traceable documentation exists for each safety topic the plan touches
(coverage). It does not prove numerical compliance; that comes from the
deterministic feasibility check.

Unknown stays unknown: any check that cannot be verified is UNKNOWN, never
PASS. ALLOW requires every check to be PASS.
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from schemas import ApprovalStatus, ImpactLevel, PlanStatus

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 3
_OK_TOOL_STATUSES = {"SUCCESS", "PARTIAL"}

# Where Tool 12 / Tool 16 live. Checked in order; first module that has the name wins.
#   Tool 12: tools.operational_tools.assess_change_impact
#   Tool 16: tools.operational_tools.retrieve_engineering_evidence
TOOL_MODULES = ("tools.operational_tools", "tools")


# ---------------------------------------------------------------------------
# Result vocabulary
# ---------------------------------------------------------------------------

class SafetyDecision(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


class CheckStatus(str, Enum):
    """Per-check outcome. Names mirror the existing NetworkValidationStatus
    vocabulary (PASS/FAIL/UNKNOWN/NOT_EVALUATED)."""
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_EVALUATED = "NOT_EVALUATED"


class Reason:
    INVALID_PLAN_ID = "INVALID_PLAN_ID"
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    DATABASE_ERROR = "DATABASE_ERROR"
    NO_HUMAN_APPROVAL = "NO_HUMAN_APPROVAL"
    APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
    PLAN_NOT_APPROVED = "PLAN_NOT_APPROVED"
    PLAN_STALE = "PLAN_STALE"
    PLAN_SUPERSEDED = "PLAN_SUPERSEDED"
    PLAN_STATUS_UNKNOWN = "PLAN_STATUS_UNKNOWN"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    CHANGE_IMPACT_MISSING = "CHANGE_IMPACT_MISSING"
    CHANGE_IMPACT_UNAVAILABLE = "CHANGE_IMPACT_UNAVAILABLE"
    CHANGE_IMPACT_INVALID = "CHANGE_IMPACT_INVALID"
    CHANGE_IMPACT_INCONCLUSIVE = "CHANGE_IMPACT_INCONCLUSIVE"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NO_PLAN_EVALUATION = "NO_PLAN_EVALUATION"
    FEASIBILITY_UNKNOWN = "FEASIBILITY_UNKNOWN"
    PLAN_INFEASIBLE = "PLAN_INFEASIBLE"
    NOT_EVALUATED_PRIOR_FAILURE = "NOT_EVALUATED_PRIOR_FAILURE"


CHECK_ORDER = (
    "PLAN_IDENTITY",
    "HUMAN_APPROVAL",
    "PLAN_LIFECYCLE",
    "CHANGE_IMPACT",
    "ENGINEERING_EVIDENCE",
    "DETERMINISTIC_VALIDATION",
)


@dataclass
class SafetyCheck:
    name: str
    status: CheckStatus
    reason_code: Optional[str] = None
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidenceRef:
    """Decision -> evidence -> specific document/chunk (audit trail)."""
    topic: str
    evidence_id: Optional[int]
    document_id: Optional[str]
    source: str
    source_type: Optional[str]
    chunk_id: Optional[str]
    section: Optional[str]
    relevance_score: Optional[float]
    excerpt: str


@dataclass
class SafetyValidationResult:
    plan_id: Any
    decision: SafetyDecision
    checks: List[SafetyCheck]
    block_reasons: List[str] = field(default_factory=list)
    requires_replan: bool = False     # Tool 12 said replan; Planner must act.
    requires_review: bool = False     # at least one check is UNKNOWN.
    evidence: List[EvidenceRef] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    validated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_allowed(self) -> bool:
        return self.decision is SafetyDecision.ALLOW

    def check(self, name: str) -> SafetyCheck:
        return next(c for c in self.checks if c.name == name)

    def to_dict(self) -> Dict[str, Any]:
        def enc(o: Any) -> Any:
            if isinstance(o, Enum):
                return o.value
            if isinstance(o, datetime):
                return o.isoformat()
            if hasattr(o, "__dataclass_fields__"):
                return {k: enc(getattr(o, k)) for k in o.__dataclass_fields__}
            if isinstance(o, dict):
                return {k: enc(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [enc(v) for v in o]
            return o
        return enc(self)


# ---------------------------------------------------------------------------
# Data access (existing DB models; injectable for tests)
# ---------------------------------------------------------------------------

class SqlAlchemyPlanDataSource:
    """Read-only access to the existing database models (database.py).

    Imports are lazy so this module can be imported without a Flask app; calls
    must run inside an application context, like the existing tools.
    """

    def get_plan(self, plan_id: int) -> Any:
        from database import Plan, db
        return db.session.get(Plan, plan_id)

    def get_approvals(self, plan_id: int) -> List[Any]:
        from database import HumanApproval
        return HumanApproval.query.filter_by(plan_id=plan_id).order_by(HumanApproval.id.asc()).all()

    def get_latest_impact(self, plan_id: int) -> Any:
        from database import ChangeImpactAssessment
        return (ChangeImpactAssessment.query.filter_by(plan_id=plan_id)
                .order_by(ChangeImpactAssessment.id.desc()).first())

    def get_latest_evaluation(self, plan_id: int) -> Any:
        from database import PlanEvaluation
        return (PlanEvaluation.query.filter_by(plan_id=plan_id)
                .order_by(PlanEvaluation.id.desc()).first())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(value: Any) -> Optional[str]:
    """Normalise str/Enum to an upper-case string; None stays None."""
    if value is None:
        return None
    return str(getattr(value, "value", value)).strip().upper()


def _unwrap(envelope: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]], List[str]]:
    """Read a tool response defensively -> (tool_status, payload, errors).

    Accepts a dict or a pydantic-like object. Anything unrecognised yields
    (None, None, [...]) which callers treat as a failed/unknown call.
    """
    if envelope is None:
        return None, None, ["Tool returned no response."]
    if not isinstance(envelope, dict) and hasattr(envelope, "model_dump"):
        envelope = envelope.model_dump()
    if not isinstance(envelope, dict):
        return None, None, [f"Unrecognised tool response type: {type(envelope).__name__}"]
    status = _norm(envelope.get("tool_status", envelope.get("status")))
    payload = envelope.get("result", envelope.get("data"))
    errors = list(envelope.get("errors") or [])
    if envelope.get("message") and status not in _OK_TOOL_STATUSES:
        errors.append(str(envelope["message"]))
    return status, payload if isinstance(payload, dict) else None, errors


def _plan_uses(plan: Any, kind: str) -> bool:
    """Does the plan dispatch this resource kind? ('generator' | 'battery')"""
    dispatch = getattr(plan, "gen_dispatch_json" if kind == "generator" else "battery_dispatch_json", None)
    if dispatch:
        return True
    try:
        return kind in json.dumps(getattr(plan, "actions", None), default=str).lower()
    except (TypeError, ValueError):
        return True  # cannot inspect -> fail safe: require the evidence


@dataclass(frozen=True)
class EvidenceTopic:
    key: str
    query: str
    topic: Optional[str]
    applies: Callable[[Any], bool]


DEFAULT_EVIDENCE_TOPICS: Tuple[EvidenceTopic, ...] = (
    EvidenceTopic("reserve", "operating reserve requirement grid code", "reserve", lambda p: True),
    EvidenceTopic("generator", "generator ramp rate minimum maximum output limits", "generator",
                  lambda p: _plan_uses(p, "generator")),
    EvidenceTopic("battery", "battery state of charge charge discharge power limits", "battery",
                  lambda p: _plan_uses(p, "battery")),
)


@dataclass
class _ImpactView:
    assessment_id: Optional[int]
    replan_required: bool
    still_valid: Optional[bool]
    impact_level: Optional[str]
    source: str


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class SafetyAgent:
    """Validation gate: ``validate_plan(plan_id)`` -> ALLOW | BLOCK.

    ``assess_change_impact_fn`` / ``retrieve_evidence_fn`` default to Tool 12 /
    Tool 16 resolved lazily from ``TOOL_MODULES`` (tools.operational_tools).
    """

    def __init__(self, data_source: Any = None, *,
                 assess_change_impact_fn: Optional[Callable[..., Any]] = None,
                 retrieve_evidence_fn: Optional[Callable[..., Any]] = None,
                 evidence_topics: Sequence[EvidenceTopic] = DEFAULT_EVIDENCE_TOPICS,
                 top_k: int = DEFAULT_TOP_K):
        self._ds = data_source if data_source is not None else SqlAlchemyPlanDataSource()
        self._tool12 = assess_change_impact_fn
        self._tool16 = retrieve_evidence_fn
        self._topics = tuple(evidence_topics)
        self._top_k = top_k

    # -- tool resolution ---------------------------------------------------

    def _resolve(self, injected: Optional[Callable[..., Any]], name: str) -> Callable[..., Any]:
        """Injected function, else ``name`` from the first module in TOOL_MODULES that has it."""
        if injected is not None:
            return injected
        problems: List[str] = []
        for module_name in TOOL_MODULES:
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:
                problems.append(f"{module_name}: {exc}")
                continue
            fn = getattr(module, name, None)
            if callable(fn):
                return fn
            problems.append(f"{module_name}: no attribute {name!r}")
        raise ImportError(f"{name} not found ({'; '.join(problems)})")

    # -- public API --------------------------------------------------------

    def validate_plan(self, plan_id: int, current_snapshot_id: Optional[int] = None) -> SafetyValidationResult:
        """Validate the exact ``plan_id``. Never executes anything.

        ``current_snapshot_id``: when given, Tool 12 is invoked to refresh the
        change-impact assessment against that snapshot. When omitted, the latest
        persisted assessment is used (and flagged as not refreshed); if none
        exists the change-impact check is UNKNOWN and the plan is blocked.
        """
        checks: List[SafetyCheck] = []
        warnings: List[str] = []
        evidence: List[EvidenceRef] = []
        state: Dict[str, Any] = {"plan": None, "requires_replan": False}

        gating = (
            ("PLAN_IDENTITY", lambda: self._check_identity(plan_id, state)),
            ("HUMAN_APPROVAL", lambda: self._check_approval(state, warnings)),
            ("PLAN_LIFECYCLE", lambda: self._check_lifecycle(state)),
            ("CHANGE_IMPACT", lambda: self._check_change_impact(state, current_snapshot_id, warnings)),
        )
        for name, run in gating:
            check = self._guard(name, run)
            checks.append(check)
            if check.status is not CheckStatus.PASS:  # short-circuit: no point going further
                break
        else:
            ev_check, refs = self._guard_evidence(state["plan"], warnings)
            checks.append(ev_check)
            evidence.extend(refs)
            checks.append(self._guard("DETERMINISTIC_VALIDATION",
                                      lambda: self._check_deterministic(state["plan"])))

        done = {c.name for c in checks}
        for name in CHECK_ORDER:
            if name not in done:
                checks.append(SafetyCheck(name, CheckStatus.NOT_EVALUATED,
                                          Reason.NOT_EVALUATED_PRIOR_FAILURE,
                                          "Skipped: an earlier gating check did not pass."))
        checks.sort(key=lambda c: CHECK_ORDER.index(c.name))

        all_pass = all(c.status is CheckStatus.PASS for c in checks)
        return SafetyValidationResult(
            plan_id=plan_id,
            decision=SafetyDecision.ALLOW if all_pass else SafetyDecision.BLOCK,
            checks=checks,
            block_reasons=[c.reason_code for c in checks
                           if c.status in (CheckStatus.FAIL, CheckStatus.UNKNOWN) and c.reason_code],
            requires_replan=state["requires_replan"],
            requires_review=any(c.status is CheckStatus.UNKNOWN for c in checks),
            evidence=evidence,
            warnings=warnings,
        )

    # -- guards: any unexpected error is UNKNOWN, never PASS ---------------

    def _guard(self, name: str, run: Callable[[], SafetyCheck]) -> SafetyCheck:
        try:
            return run()
        except Exception as exc:  # noqa: BLE001 - failure to verify must stay visible
            logger.warning("Safety check %s could not be completed: %s", name, exc)
            return SafetyCheck(name, CheckStatus.UNKNOWN, Reason.DATABASE_ERROR,
                               f"{name} could not be verified: {exc}")

    def _guard_evidence(self, plan: Any, warnings: List[str]) -> Tuple[SafetyCheck, List[EvidenceRef]]:
        try:
            return self._check_evidence(plan, warnings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evidence check failed: %s", exc)
            return (SafetyCheck("ENGINEERING_EVIDENCE", CheckStatus.UNKNOWN, Reason.RETRIEVAL_FAILED,
                                f"Engineering evidence could not be verified: {exc}"), [])

    # -- individual checks -------------------------------------------------

    def _check_identity(self, plan_id: Any, state: Dict[str, Any]) -> SafetyCheck:
        name = "PLAN_IDENTITY"
        if isinstance(plan_id, bool) or not isinstance(plan_id, int):
            return SafetyCheck(name, CheckStatus.FAIL, Reason.INVALID_PLAN_ID,
                               "plan_id must be an integer.")
        plan = self._ds.get_plan(plan_id)
        if plan is None:
            return SafetyCheck(name, CheckStatus.FAIL, Reason.PLAN_NOT_FOUND, f"Plan {plan_id} not found.")
        state["plan"] = plan
        return SafetyCheck(name, CheckStatus.PASS, message=f"Plan {plan_id} found.")

    def _check_approval(self, state: Dict[str, Any], warnings: List[str]) -> SafetyCheck:
        name, plan = "HUMAN_APPROVAL", state["plan"]
        rows = list(self._ds.get_approvals(plan.id))
        exact = [r for r in rows if getattr(r, "plan_id", None) == plan.id]
        if len(exact) != len(rows):
            warnings.append("Ignored approval records that do not belong to this plan.")
        if not exact:
            return SafetyCheck(name, CheckStatus.FAIL, Reason.NO_HUMAN_APPROVAL,
                               "No Human Approval record exists for this exact plan.")
        latest = exact[-1]  # most recent record decides
        if len(exact) > 1:
            warnings.append("Multiple approval records found; the most recent one was used.")
        status = _norm(getattr(latest, "status", None))
        if status != ApprovalStatus.APPROVED.value:
            return SafetyCheck(name, CheckStatus.FAIL, Reason.APPROVAL_NOT_APPROVED,
                               f"Latest Human Approval status is {status}.", {"approval_status": status})
        if getattr(latest, "reviewed_at", None) is None:
            warnings.append("Approval is APPROVED but has no reviewed_at timestamp.")
        return SafetyCheck(name, CheckStatus.PASS, message="Exact plan has an APPROVED Human Approval record.",
                           details={"approval_id": getattr(latest, "id", None)})

    def _check_lifecycle(self, state: Dict[str, Any]) -> SafetyCheck:
        name, plan = "PLAN_LIFECYCLE", state["plan"]
        status = _norm(getattr(plan, "status", None))
        if status == PlanStatus.APPROVED.value:
            return SafetyCheck(name, CheckStatus.PASS, message="Plan status is APPROVED.")
        reasons = {PlanStatus.STALE.value: Reason.PLAN_STALE,
                   PlanStatus.SUPERSEDED.value: Reason.PLAN_SUPERSEDED}
        if status in {s.value for s in PlanStatus}:
            return SafetyCheck(name, CheckStatus.FAIL, reasons.get(status, Reason.PLAN_NOT_APPROVED),
                               f"Plan status is {status}; APPROVED is required.", {"plan_status": status})
        return SafetyCheck(name, CheckStatus.UNKNOWN, Reason.PLAN_STATUS_UNKNOWN,
                           f"Plan status {status!r} is not a recognised lifecycle state.")

    def _impact_from_row(self, row: Any, source: str) -> _ImpactView:
        return _ImpactView(getattr(row, "id", None), bool(row.replan_required),
                           getattr(row, "still_valid", None), _norm(getattr(row, "impact_level", None)), source)

    def _check_change_impact(self, state: Dict[str, Any], snapshot_id: Optional[int],
                             warnings: List[str]) -> SafetyCheck:
        name, plan = "CHANGE_IMPACT", state["plan"]
        before = self._ds.get_latest_impact(plan.id)
        view: Optional[_ImpactView] = None

        if snapshot_id is not None:
            try:
                tool12 = self._resolve(self._tool12, "assess_change_impact")
            except ImportError as exc:
                return SafetyCheck(name, CheckStatus.UNKNOWN, Reason.TOOL_UNAVAILABLE,
                                   f"Tool 12 is not available: {exc}")
            status, payload, errors = _unwrap(tool12(plan_id=plan.id, current_snapshot_id=snapshot_id))
            if status not in _OK_TOOL_STATUSES:
                return SafetyCheck(name, CheckStatus.UNKNOWN, Reason.CHANGE_IMPACT_UNAVAILABLE,
                                   "Tool 12 did not return a usable assessment.",
                                   {"tool_status": status, "errors": errors})
            after = self._ds.get_latest_impact(plan.id)
            if after is not None and (before is None or getattr(after, "id", None) != getattr(before, "id", None)):
                view = self._impact_from_row(after, "TOOL_12_PERSISTED")
            elif payload is not None and isinstance(payload.get("replan_required"), bool):
                view = _ImpactView(None, payload["replan_required"], payload.get("still_valid"),
                                   _norm(payload.get("impact_level")), "TOOL_12_RESPONSE")
            else:
                return SafetyCheck(name, CheckStatus.UNKNOWN, Reason.CHANGE_IMPACT_UNAVAILABLE,
                                   "Tool 12 succeeded but produced no identifiable assessment.")
        else:
            if before is None:
                return SafetyCheck(name, CheckStatus.UNKNOWN, Reason.CHANGE_IMPACT_MISSING,
                                   "No Change Impact Assessment exists and no current snapshot was supplied "
                                   "to request one from Tool 12.")
            view = self._impact_from_row(before, "PERSISTED")
            warnings.append("Change impact not refreshed: using latest persisted assessment.")

        details = {"assessment_id": view.assessment_id, "source": view.source,
                   "replan_required": view.replan_required, "still_valid": view.still_valid,
                   "impact_level": view.impact_level}
        if view.replan_required:
            state["requires_replan"] = True  # Planner/lifecycle layer handles this; not us.
            return SafetyCheck(name, CheckStatus.FAIL, Reason.REPLAN_REQUIRED,
                               "Tool 12 requires replanning; this plan must not proceed.", details)
        if view.still_valid is False:
            return SafetyCheck(name, CheckStatus.FAIL, Reason.CHANGE_IMPACT_INVALID,
                               "Change impact assessment marks the plan as no longer valid.", details)
        if view.still_valid is None or view.impact_level in (None, ImpactLevel.UNKNOWN.value):
            return SafetyCheck(name, CheckStatus.UNKNOWN, Reason.CHANGE_IMPACT_INCONCLUSIVE,
                               "Change impact is inconclusive (validity or impact level unknown).", details)
        return SafetyCheck(name, CheckStatus.PASS, message="No replanning required.", details=details)

    def _check_evidence(self, plan: Any, warnings: List[str]) -> Tuple[SafetyCheck, List[EvidenceRef]]:
        name = "ENGINEERING_EVIDENCE"
        try:
            tool16 = self._resolve(self._tool16, "retrieve_engineering_evidence")
        except ImportError as exc:
            return (SafetyCheck(name, CheckStatus.UNKNOWN, Reason.TOOL_UNAVAILABLE,
                                f"Tool 16 is not available: {exc}"), [])

        refs: List[EvidenceRef] = []
        topics: Dict[str, Dict[str, Any]] = {}
        for t in self._topics:
            try:
                needed = t.applies(plan)
            except Exception:  # noqa: BLE001 - cannot decide -> require the evidence
                needed = True
            if not needed:
                continue
            topic_refs, entry = self._retrieve_topic(tool16, plan, t)
            topics[t.key] = entry
            refs.extend(topic_refs)

        if not topics:
            return (SafetyCheck(name, CheckStatus.UNKNOWN, Reason.INSUFFICIENT_EVIDENCE,
                                "No evidence topics were configured for this plan."), [])

        bad = {k: v for k, v in topics.items() if v["status"] != CheckStatus.PASS.value}
        if bad:
            codes = {v["reason_code"] for v in bad.values()}
            code = Reason.RETRIEVAL_FAILED if Reason.RETRIEVAL_FAILED in codes else Reason.INSUFFICIENT_EVIDENCE
            return (SafetyCheck(name, CheckStatus.UNKNOWN, code,
                                "Engineering evidence is missing or unverifiable for: " + ", ".join(sorted(bad)),
                                {"topics": topics}), refs)
        if refs and all(r.source_type == "synthetic" for r in refs):
            warnings.append("Engineering evidence is synthetic (demo) documentation only.")
        return (SafetyCheck(name, CheckStatus.PASS, message="Traceable engineering evidence found for all topics.",
                            details={"topics": topics}), refs)

    def _retrieve_topic(self, tool16: Callable[..., Any], plan: Any,
                        t: EvidenceTopic) -> Tuple[List[EvidenceRef], Dict[str, Any]]:
        def fail(code: str, msg: str) -> Tuple[List[EvidenceRef], Dict[str, Any]]:
            return [], {"status": CheckStatus.UNKNOWN.value, "reason_code": code, "message": msg, "evidence_count": 0}

        try:
            envelope = tool16(query=t.query, top_k=self._top_k, plan_id=plan.id, topic=t.topic)
        except Exception as exc:  # noqa: BLE001
            return fail(Reason.RETRIEVAL_FAILED, f"Tool 16 raised: {exc}")
        status, payload, errors = _unwrap(envelope)
        if status not in _OK_TOOL_STATUSES or payload is None:
            return fail(Reason.RETRIEVAL_FAILED, f"Tool 16 status {status}: {'; '.join(errors) or 'no payload'}")
        results = payload.get("results")
        if not isinstance(results, list):
            return fail(Reason.RETRIEVAL_FAILED, "Tool 16 payload has no results list.")

        refs = [r for r in (self._to_ref(t.key, item) for item in results) if r is not None]
        if not refs:
            msg = ("No evidence returned." if not results
                   else "Evidence returned but malformed (missing source or text).")
            return fail(Reason.INSUFFICIENT_EVIDENCE, msg)
        return refs, {"status": CheckStatus.PASS.value, "reason_code": None,
                      "evidence_count": len(refs), "query": t.query}

    @staticmethod
    def _to_ref(topic: str, item: Any) -> Optional[EvidenceRef]:
        """Keep only traceable evidence: must name a source and carry text."""
        if not isinstance(item, dict):
            return None
        source = item.get("source") or item.get("document_source")
        text = item.get("text") or item.get("chunk_text")
        if not source or not isinstance(text, str) or not text.strip():
            return None
        meta = item.get("document_metadata") or item.get("doc_metadata") or {}
        return EvidenceRef(
            topic=topic, evidence_id=item.get("evidence_id"), document_id=item.get("document_id"),
            source=str(source), source_type=item.get("source_type"), chunk_id=item.get("chunk_id"),
            section=meta.get("section") if isinstance(meta, dict) else None,
            relevance_score=item.get("relevance_score"), excerpt=text.strip()[:300],
        )

    def _check_deterministic(self, plan: Any) -> SafetyCheck:
        """Read the persisted Tool 09 evaluation; nothing is recomputed here."""
        name = "DETERMINISTIC_VALIDATION"
        ev = self._ds.get_latest_evaluation(plan.id)
        if ev is None:
            return SafetyCheck(name, CheckStatus.UNKNOWN, Reason.NO_PLAN_EVALUATION,
                               "No persisted plan evaluation exists; feasibility is unverified.")
        details = {"evaluation_id": getattr(ev, "id", None), "is_feasible": ev.is_feasible,
                   "reserve_margin_mw": getattr(ev, "reserve_margin_mw", None)}
        pf = getattr(ev, "power_flow_result", None)
        pf_bad = isinstance(pf, dict) and (pf.get("is_network_feasible") is False or pf.get("converged") is False)
        if ev.is_feasible is False or getattr(ev, "constraint_violations", None) or pf_bad:
            return SafetyCheck(name, CheckStatus.FAIL, Reason.PLAN_INFEASIBLE,
                               "Persisted evaluation reports infeasibility, constraint or network violations.",
                               details)
        if ev.is_feasible is None:
            return SafetyCheck(name, CheckStatus.UNKNOWN, Reason.FEASIBILITY_UNKNOWN,
                               "Plan feasibility is UNKNOWN in the persisted evaluation.", details)
        return SafetyCheck(name, CheckStatus.PASS, message="Persisted evaluation reports the plan feasible.",
                           details=details)
