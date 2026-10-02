"""Shared Team 2 runtime helpers.

The runtime owns validation, logging, transaction boundaries and status
separation. It contains no planning or physical policy logic.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, TypeVar

from schemas import ToolStatus

T = TypeVar("T")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_run_id(db, AgentRunTrace, run_id: int | None = None, trigger_source: str = "TOOL_CALL") -> int:
    if run_id is not None:
        row = db.session.get(AgentRunTrace, run_id)
        if row is None:
            raise ValueError(f"Unknown run_id: {run_id}")
        return row.id
    row = AgentRunTrace(run_uuid=str(uuid.uuid4()), trigger_source=trigger_source, status="RUNNING")
    db.session.add(row)
    db.session.flush()
    return row.id


def next_step_index(AgentRunTrace, ToolCallLog, run_id: int) -> int:
    query = ToolCallLog.query.filter_by(run_id=run_id)
    step_attr = getattr(ToolCallLog, "step_index", None)
    if step_attr is not None and hasattr(step_attr, "desc"):
        query = query.order_by(step_attr.desc())
    last = query.first()
    return 0 if last is None else last.step_index + 1


@contextmanager
def tool_call(db, AgentRunTrace, ToolCallLog, *, tool_name: str, tool_category: str,
              input_payload: dict[str, Any], run_id: int | None = None,
              trigger_source: str = "TOOL_CALL"):
    """Yield a logging context. The caller commits or rolls back its work."""
    rid = ensure_run_id(db, AgentRunTrace, run_id, trigger_source)
    step = next_step_index(AgentRunTrace, ToolCallLog, rid)
    log = ToolCallLog(
        run_id=rid,
        step_index=step,
        tool_name=tool_name,
        tool_category=tool_category,
        input_json=json.loads(json.dumps(input_payload, default=str)),
        status=ToolStatus.SUCCESS.value,
    )
    db.session.add(log)
    db.session.flush()
    started = time.perf_counter()
    try:
        yield rid, log
    except Exception as exc:
        db.session.rollback()
        # Rollback removes the pending log in many SQLAlchemy configurations;
        # recreate it in a fresh transaction so rejected/failed calls remain auditable.
        rid = ensure_run_id(db, AgentRunTrace, rid, trigger_source)
        log = ToolCallLog(
            run_id=rid,
            step_index=next_step_index(AgentRunTrace, ToolCallLog, rid),
            tool_name=tool_name,
            tool_category=tool_category,
            input_json=json.loads(json.dumps(input_payload, default=str)),
            output_json=None,
            status=ToolStatus.FAILED.value,
            error_message=str(exc),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        db.session.add(log)
        db.session.commit()
        raise
    else:
        log.latency_ms = int((time.perf_counter() - started) * 1000)
        db.session.flush()


def finish_log(log, output_payload: dict[str, Any], status: ToolStatus = ToolStatus.SUCCESS,
               error_message: str | None = None) -> None:
    log.output_json = output_payload
    log.status = status.value
    log.error_message = error_message


def as_dict(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model
