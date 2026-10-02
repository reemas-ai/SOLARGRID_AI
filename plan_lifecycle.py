"""Plan lifecycle transitions used by the integration boundary.

Team 2 creates PROPOSED plans and never bypasses lifecycle gates.
"""
from __future__ import annotations

ALLOWED_TRANSITIONS = {
    None: {"PROPOSED"},
    "PROPOSED": {"REJECTED", "APPROVED", "STALE"},
    "APPROVED": {"EXECUTED", "STALE"},
    "STALE": {"SUPERSEDED"},
    "REJECTED": set(),
    "SUPERSEDED": set(),
    "EXECUTED": set(),
}


def transition_plan(plan, target_status: str) -> None:
    current = plan.status
    if target_status not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid plan transition: {current!r} -> {target_status!r}")
    plan.status = target_status
