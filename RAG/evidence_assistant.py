"""
SOLARGRID AI - Engineering Evidence Assistant (RAG -> LLM, behind Tool 16).

    question
       |
       v
    Tool 16  retrieve_engineering_evidence   <- the ONLY retrieval path
       |        (which delegates to RAG/rag_engine.py)
       v
    numbered evidence blocks [E1]..[En]
       |   no usable evidence?  ->  stop, the LLM is NOT called
       v
    LLM  (system prompt = RAG/prompts/engineering_evidence_prompt.md)
       |
       v
    verification (citations, numbers, decision language)
       |
       v
    AssistantAnswer  status = GROUNDED | UNVERIFIED | INSUFFICIENT_EVIDENCE |
                              RETRIEVAL_FAILED | LLM_FAILED | INVALID_QUESTION

Boundaries
----------
* Explanation only. The answer is NEVER an approval, a safety decision or a
  plan change. ALLOW / BLOCK belongs to agents/safety_agent.py, which does not
  use this module, and this module does not import it.
* It never imports rag_engine, the database or any execution tool. Evidence
  arrives only through Tool 16 (resolved lazily like the Safety Agent does).
* Live operational values are not available to it (see the prompt, rule 3).
* Verification is heuristic and conservative: anything it cannot confirm is
  reported as UNVERIFIED with the reasons in ``issues``; it is never upgraded.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from agents.prompts import RAG_SYSTEM_PROMPT, RAG_STRUCT_RULES

logger = logging.getLogger(__name__)

PROMPT_PATH = None  # legacy argument retained; canonical prompt lives in agents.prompts
CANONICAL_USER_TEMPLATE = "Evidence blocks:\n\n{evidence}\n\nQuestion:\n{question}\n\nAnswer using only the evidence blocks above with [E#] citations. If they do not contain the answer, start with \"INSUFFICIENT EVIDENCE:\"."

TOOL_MODULES = ("tools.operational_tools", "tools")   # same lookup order as the Safety Agent
LLM_MODULE, LLM_FUNCTION = "llm", "call_llm"          # same entry point rag.py used
DEFAULT_TOP_K = 5
DEFAULT_MAX_CONTEXT_CHARS = 12000
INSUFFICIENT_MARKER = "INSUFFICIENT EVIDENCE"
_OK_TOOL_STATUSES = {"SUCCESS", "PARTIAL"}

_SYSTEM_MARK, _USER_MARK = "=== SYSTEM ===", "=== USER ==="
_END_MARK = "-----END EVIDENCE"
_BEGIN_MARK = "-----BEGIN EVIDENCE"


class PromptError(Exception):
    """The prompt file is missing or malformed."""


class AnswerStatus(str, Enum):
    GROUNDED = "GROUNDED"                          # cited, every citation valid, no issues found
    UNVERIFIED = "UNVERIFIED"                      # LLM answered, but checks found issues
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    LLM_FAILED = "LLM_FAILED"
    INVALID_QUESTION = "INVALID_QUESTION"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromptTemplate:
    system: str
    user_template: str
    sha256: str          # audit: which exact prompt produced an answer


_SYSTEM_LINE_RE = re.compile(r"(?m)^=== SYSTEM ===[ \t\r]*$")
_USER_LINE_RE = re.compile(r"(?m)^=== USER ===[ \t\r]*$")


def load_prompt(path: Path | str | None = None) -> PromptTemplate:
    """Return the canonical RAG prompt from agents.prompts; never read a duplicate file."""
    raw = RAG_SYSTEM_PROMPT.strip() + "\n\n" + RAG_STRUCT_RULES.strip()
    return PromptTemplate(raw, CANONICAL_USER_TEMPLATE, hashlib.sha256(raw.encode("utf-8")).hexdigest())


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class EvidenceItem:
    label: str                       # "E1", "E2", ... as the LLM sees it
    text: str
    source: str
    evidence_id: Optional[int] = None
    document_id: Optional[str] = None
    chunk_id: Optional[str] = None
    section: Optional[str] = None
    source_type: Optional[str] = None
    relevance_score: Optional[float] = None


@dataclass
class AssistantAnswer:
    status: AnswerStatus
    question: str
    answer: Optional[str] = None                 # LLM text (None when the LLM was not called / failed)
    message: str = ""                            # deterministic explanation for non-answers
    cited: List[str] = field(default_factory=list)          # labels the answer cites, e.g. ["E1"]
    evidence: List[EvidenceItem] = field(default_factory=list)   # everything the LLM was shown
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    llm_called: bool = False
    evidence_quality: Optional[str] = None
    truncated_evidence: int = 0
    prompt_sha256: Optional[str] = None

    @property
    def is_grounded(self) -> bool:
        return self.status is AnswerStatus.GROUNDED

    def cited_sources(self) -> List[EvidenceItem]:
        return [e for e in self.evidence if e.label in set(self.cited)]

    def to_dict(self) -> Dict[str, Any]:
        def enc(o: Any) -> Any:
            if isinstance(o, Enum):
                return o.value
            if hasattr(o, "__dataclass_fields__"):
                return {k: enc(getattr(o, k)) for k in o.__dataclass_fields__}
            if isinstance(o, (list, tuple)):
                return [enc(v) for v in o]
            if isinstance(o, dict):
                return {k: enc(v) for k, v in o.items()}
            return o
        return enc(self)


# ---------------------------------------------------------------------------
# Helpers (tool envelope, evidence blocks, LLM response, verification)
# ---------------------------------------------------------------------------

def _norm_status(value: Any) -> Optional[str]:
    return None if value is None else str(getattr(value, "value", value)).strip().upper()


def _unwrap(envelope: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]], List[str]]:
    """Read a tool response -> (tool_status, payload, errors). Mirrors the Safety Agent's parser."""
    if envelope is None:
        return None, None, ["Tool returned no response."]
    if not isinstance(envelope, dict) and hasattr(envelope, "model_dump"):
        envelope = envelope.model_dump()
    if not isinstance(envelope, dict):
        return None, None, [f"Unrecognised tool response type: {type(envelope).__name__}"]
    status = _norm_status(envelope.get("tool_status", envelope.get("status")))
    payload = envelope.get("result", envelope.get("data"))
    errors = list(envelope.get("errors") or [])
    if envelope.get("message") and status not in _OK_TOOL_STATUSES:
        errors.append(str(envelope["message"]))
    return status, payload if isinstance(payload, dict) else None, errors


def _to_items(results: Sequence[Any]) -> List[EvidenceItem]:
    """Keep only traceable evidence (must name a source and carry text). Labels are assigned in order."""
    items: List[EvidenceItem] = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        source = raw.get("source") or raw.get("document_source")
        text = raw.get("text") or raw.get("chunk_text")
        if not source or not isinstance(text, str) or not text.strip():
            continue
        meta = raw.get("document_metadata") or raw.get("doc_metadata") or {}
        items.append(EvidenceItem(
            label=f"E{len(items) + 1}", text=text.strip(), source=str(source),
            evidence_id=raw.get("evidence_id"), document_id=raw.get("document_id"),
            chunk_id=raw.get("chunk_id"),
            section=meta.get("section") if isinstance(meta, dict) else None,
            source_type=raw.get("source_type"), relevance_score=raw.get("relevance_score"),
        ))
    return items


def _neutralise(text: str) -> str:
    """Stop document text from forging evidence-block boundaries."""
    return text.replace(_END_MARK, "[marker removed]").replace(_BEGIN_MARK, "[marker removed]")


def build_evidence_context(items: List[EvidenceItem], max_chars: int) -> Tuple[str, List[EvidenceItem], int]:
    """Render evidence blocks within a character budget -> (context, items_shown, dropped_count)."""
    blocks: List[str] = []
    shown: List[EvidenceItem] = []
    used = 0
    for item in items:
        header = (f"[{item.label}] source: {item.source} | section: {item.section or 'n/a'} | "
                  f"chunk_id: {item.chunk_id or 'n/a'} | source_type: {item.source_type or 'unknown'}")
        body = _neutralise(item.text)
        block = f"{header}\n{_BEGIN_MARK} {item.label}-----\n{body}\n{_END_MARK} {item.label}-----"
        if used + len(block) > max_chars:
            if shown:
                break                                   # budget reached: drop lower-ranked evidence
            room = max(0, max_chars - len(header) - 120)   # first block alone too big: truncate it
            body = body[:room] + " ...[truncated]"
            block = f"{header}\n{_BEGIN_MARK} {item.label}-----\n{body}\n{_END_MARK} {item.label}-----"
            item = EvidenceItem(**{**item.__dict__, "text": body})
        blocks.append(block)
        shown.append(item)
        used += len(block) + 2
    return "\n\n".join(blocks), shown, len(items) - len(shown)


def _llm_text(response: Any) -> Optional[str]:
    """Text from a str, an OpenAI-style object (.choices[0].message.content) or the same as a dict."""
    if isinstance(response, str):
        return response
    try:
        choices = response["choices"] if isinstance(response, dict) else response.choices
        first = choices[0]
        message = first["message"] if isinstance(first, dict) else first.message
        content = message["content"] if isinstance(message, dict) else message.content
        return content if isinstance(content, str) else None
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_CITE_GROUP_RE = re.compile(r"\[([^\[\]]*\bE\d+[^\[\]]*)\]")
_CITE_ID_RE = re.compile(r"E(\d+)")
_ID_WITH_HASH_RE = re.compile(r"[\w.\-]+#\d+")
_LIST_MARKER_RE = re.compile(r"(?m)^\s*(?:[-*]\s*)?\d+[.)]\s")
_NUMBER_RE = re.compile(r"(?<![\w#])\d+(?:[.,]\d+)*")
_DECISION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(?:plan|it|this|that)\s+(?:is|was|has\s+been)\s+(?:approved|safe|compliant|valid|cleared)\b",
    r"\bsafe\s+to\s+(?:execute|proceed|run|apply)\b",
    r"\bapproved\s+for\s+execution\b",
    r"\bI\s+(?:approve|certify|clear)\b",
    r"(?:الخطة|الخطه)\s+(?:آمنة|امنة|سليمة|مقبولة|معتمدة)",
    r"(?:آمنة|امنة)\s+(?:للتنفيذ|للتشغيل)",
    r"تمت\s+الموافقة",
)] + [re.compile(r"\b(?:ALLOW|BLOCK)(?:ED)?\b")]          # case-sensitive on purpose


def _numbers(text: str) -> Set[float]:
    out: Set[float] = set()
    for tok in _NUMBER_RE.findall(text.translate(_AR_DIGITS)):
        try:
            out.add(float(tok.replace(",", "")))
        except ValueError:
            continue
    return out


def _answer_numbers(answer: str) -> Set[float]:
    """Numbers the LLM itself introduced (citations, list markers, chunk ids excluded)."""
    text = _CITE_GROUP_RE.sub(" ", answer.translate(_AR_DIGITS))
    text = _ID_WITH_HASH_RE.sub(" ", _LIST_MARKER_RE.sub("\n", text))
    return _numbers(text)


def verify_answer(answer: str, items: List[EvidenceItem]) -> Tuple[List[str], List[str], List[str]]:
    """-> (cited_labels, issues, ungrounded_number_strings). Heuristic and conservative."""
    issues: List[str] = []
    known = {item.label for item in items}
    cited: List[str] = []
    for group in _CITE_GROUP_RE.findall(answer):
        for num in _CITE_ID_RE.findall(group):
            label = f"E{num}"
            if label not in cited:
                cited.append(label)
    if not cited:
        issues.append("NO_CITATIONS: the answer cites no evidence block.")
    unknown = [c for c in cited if c not in known]
    if unknown:
        issues.append(f"UNKNOWN_CITATION: cites blocks that were not provided: {', '.join(unknown)}.")
    cited = [c for c in cited if c in known]

    evidence_numbers: Set[float] = set()
    for item in items:
        evidence_numbers |= _numbers(item.text)
    ungrounded = sorted(n for n in _answer_numbers(answer) if n not in evidence_numbers)
    shown = [f"{n:g}" for n in ungrounded]
    if ungrounded:
        issues.append(f"UNGROUNDED_NUMBER: numbers not found in the evidence: {', '.join(shown)}.")

    hits = sorted({m.group(0) for pat in _DECISION_PATTERNS for m in pat.finditer(answer)})
    if hits:
        issues.append(f"DECISION_LANGUAGE: the answer sounds like an approval/safety decision: {hits}.")
    return cited, issues, shown


# ---------------------------------------------------------------------------
# Assistant
# ---------------------------------------------------------------------------

class EvidenceAssistant:
    """Answer engineering questions from Tool 16 evidence only.

    ``retrieve_evidence_fn`` / ``llm_fn`` are injectable; by default Tool 16 is
    resolved from ``TOOL_MODULES`` and the LLM from ``llm.call_llm``.
    """

    def __init__(self, *, retrieve_evidence_fn: Optional[Callable[..., Any]] = None,
                 llm_fn: Optional[Callable[[List[Dict[str, str]]], Any]] = None,
                 prompt_path: Path | str | None = PROMPT_PATH, top_k: int = DEFAULT_TOP_K,
                 max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS):
        self._tool16 = retrieve_evidence_fn
        self._llm = llm_fn
        self._prompt = load_prompt(prompt_path)          # canonical prompt source: agents.prompts
        self._top_k = top_k
        self._max_chars = max_context_chars

    # -- lazy resolution ---------------------------------------------------

    def _resolve_tool16(self) -> Callable[..., Any]:
        if self._tool16 is not None:
            return self._tool16
        problems: List[str] = []
        for module_name in TOOL_MODULES:
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:
                problems.append(f"{module_name}: {exc}")
                continue
            fn = getattr(module, "retrieve_engineering_evidence", None)
            if callable(fn):
                return fn
            problems.append(f"{module_name}: no attribute 'retrieve_engineering_evidence'")
        raise ImportError("Tool 16 not found (" + "; ".join(problems) + ")")

    def _resolve_llm(self) -> Callable[[List[Dict[str, str]]], Any]:
        if self._llm is not None:
            return self._llm
        return getattr(importlib.import_module(LLM_MODULE), LLM_FUNCTION)

    # -- public API --------------------------------------------------------

    def answer(self, question: str, *, plan_id: Optional[int] = None, topic: Optional[str] = None,
               top_k: Optional[int] = None) -> AssistantAnswer:
        """Retrieve via Tool 16, ask the LLM, verify. Never raises for expected failures."""
        sha = self._prompt.sha256
        if not isinstance(question, str) or not question.strip():
            return AssistantAnswer(AnswerStatus.INVALID_QUESTION, str(question or ""),
                                   message="The question is empty or not text.", prompt_sha256=sha)
        question = question.strip()

        # 1) Evidence, through Tool 16 only.
        try:
            tool16 = self._resolve_tool16()
            envelope = tool16(query=question, top_k=top_k or self._top_k, plan_id=plan_id, topic=topic)
        except Exception as exc:  # noqa: BLE001 - retrieval failure must stay visible
            logger.warning("Tool 16 call failed: %s", exc)
            return AssistantAnswer(AnswerStatus.RETRIEVAL_FAILED, question, prompt_sha256=sha,
                                   message=f"Engineering evidence could not be retrieved: {exc}")
        status, payload, errors = _unwrap(envelope)
        if status not in _OK_TOOL_STATUSES or payload is None or not isinstance(payload.get("results"), list):
            return AssistantAnswer(AnswerStatus.RETRIEVAL_FAILED, question, prompt_sha256=sha,
                                   message=f"Tool 16 did not return usable evidence (status {status}): "
                                           f"{'; '.join(errors) or 'no results list'}")
        quality = payload.get("evidence_quality")
        items = _to_items(payload["results"])
        if not items:
            return AssistantAnswer(
                AnswerStatus.INSUFFICIENT_EVIDENCE, question, prompt_sha256=sha, evidence_quality=quality,
                message="No sufficient documented engineering evidence was found; the LLM was not called.")

        # 2) LLM, shown only the retrieved evidence.
        context, shown, dropped = build_evidence_context(items, self._max_chars)
        messages = [
            {"role": "system", "content": self._prompt.system},
            {"role": "user", "content": self._prompt.user_template
                .replace("{evidence}", context).replace("{question}", question)},
        ]
        base = dict(question=question, evidence=shown, evidence_quality=quality,
                    truncated_evidence=dropped, prompt_sha256=sha)
        warnings = [f"{dropped} lower-ranked evidence block(s) omitted (context budget)."] if dropped else []
        try:
            text = _llm_text(self._resolve_llm()(messages))
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM call failed: %s", exc)
            return AssistantAnswer(AnswerStatus.LLM_FAILED, llm_called=True, warnings=warnings,
                                   message=f"The LLM call failed: {exc}", **base)
        if text is None or not text.strip():
            return AssistantAnswer(AnswerStatus.LLM_FAILED, llm_called=True, warnings=warnings,
                                   message="The LLM returned no usable text.", **base)
        text = text.strip()

        # 3) Verification.
        if text.upper().startswith(INSUFFICIENT_MARKER):
            return AssistantAnswer(AnswerStatus.INSUFFICIENT_EVIDENCE, answer=text, llm_called=True,
                                   warnings=warnings, message="The LLM reports the evidence is insufficient.",
                                   **base)
        cited, issues, _ = verify_answer(text, shown)
        if any(e.source_type == "synthetic" for e in shown if e.label in cited):
            warnings.append("Answer relies on synthetic (demo) documentation.")
        final = AnswerStatus.GROUNDED if not issues else AnswerStatus.UNVERIFIED
        return AssistantAnswer(final, answer=text, cited=cited, issues=issues, llm_called=True,
                               warnings=warnings, **base)
