"""Scheduled/event-triggered monitor that stops at Human Approval.

The service may assess, detect, plan and validate automatically.  It never
approves a plan and never calls Tool 13 automatically.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from config_loader import load_json
from database import (
    AgentRunTrace, ExternalDataProvenance, MonitoringTick, OperationalEvent,
    Plan, ReserveAssessment, SystemSnapshot, db,
)
from agents.memory import SystemMemoryManager
from agents.planner_agent import PlannerAgent, PlannerLimits
from agents.tool_dispatcher import ToolContext
from external_data.service import ExternalDataService

from .event_detector import DetectedEvent, detect_events, state_signature


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _read(value: Any, key: str, default=None):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        data = value.get("data")
        if isinstance(data, dict) and key in data:
            return data[key]
    return default


class SolarGridMonitorService:
    def __init__(self, app, config: dict[str, Any] | None = None):
        self.app = app
        self.config = config or load_json("automation_v1.json")
        self._scheduler = None
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.started_at: datetime | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._scheduler is not None:
            return True
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except Exception as exc:
            self.last_error = f"APScheduler unavailable: {exc}"
            return False
        interval = max(60, int(self.config.get("poll_interval_seconds", 300)))
        scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
        scheduler.add_job(
            self.run_cycle,
            "interval",
            seconds=interval,
            id="solargrid-autonomous-monitor",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        self._scheduler = scheduler
        self.started_at = datetime.now(timezone.utc)
        if bool(self.config.get("run_on_startup", True)):
            threading.Thread(target=self.run_cycle, name="solargrid-monitor-initial", daemon=True).start()
        return True

    def shutdown(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            finally:
                self._scheduler = None

    def run_cycle(self, *, trigger_source: str = "SCHEDULED_MONITOR") -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"status": "SKIPPED", "reason": "A monitor cycle is already running."}
        try:
            with self.app.app_context():
                return self._run_cycle_in_context(trigger_source=trigger_source)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            try:
                self.app.logger.exception("Autonomous monitor cycle failed: %s", exc)
            except Exception:
                pass
            return {"status": "FAILED", "reason": self.last_error}
        finally:
            self._lock.release()

    def _run_cycle_in_context(self, *, trigger_source: str) -> dict[str, Any]:
        memory = SystemMemoryManager()
        run = memory.create_agent_run_trace(trigger_source)
        # The autonomous cycle legitimately needs more than the Planner's generic
        # 16-call interactive budget: Tools 01-04 + Tool 17 + Tool 05 +
        # three validation calls for each of four candidates + Tool 09.
        # Keep this automation-specific budget bounded and configurable rather
        # than weakening the default Planner limit used by other workflows.
        limit_cfg = self.config.get("planner_limits") or {}
        default_limits = PlannerLimits()
        planner = PlannerAgent(
            context=ToolContext(db=db, session=db.session, run_id=run.id),
            limits=PlannerLimits(
                max_steps=max(1, int(limit_cfg.get("max_steps", default_limits.max_steps))),
                max_replans=max(0, int(limit_cfg.get("max_replans", default_limits.max_replans))),
                max_tool_calls=max(1, int(limit_cfg.get("max_tool_calls", 24))),
            ),
        )
        horizon = max(15, int(self.config.get("horizon_minutes", 120)))
        external_ids: list[int] = []
        try:
            open_meteo_record = None
            ext_cfg = load_json("external_data_v1.json")
            external_service = ExternalDataService(ext_cfg)
            if (ext_cfg.get("open_meteo") or {}).get("enabled", True):
                open_meteo_record = external_service.refresh(
                    "OPEN_METEO", consumed_by="AUTONOMOUS_MONITOR", run_id=run.id
                )
                external_ids.append(open_meteo_record.id)

            # Lower-frequency reference sources improve provenance/observability
            # without turning a five-minute monitor into an API hammer.
            for source_name, config_key in (("NASA_POWER", "nasa_power"), ("US_EIA", "eia")):
                source_cfg = ext_cfg.get(config_key) or {}
                if source_cfg.get("enabled", True) is False:
                    continue
                latest = external_service.latest(source_name)
                refresh_minutes = max(15, int(source_cfg.get("refresh_minutes", 360)))
                stale_reference = latest is None or latest.retrieved_at is None or (
                    _utcnow_naive() - latest.retrieved_at >= timedelta(minutes=refresh_minutes)
                )
                if stale_reference:
                    reference = external_service.refresh(
                        source_name, consumed_by="AUTONOMOUS_MONITOR_REFERENCE", run_id=run.id
                    )
                    external_ids.append(reference.id)

            assessment = planner.assess_current_state(
                horizon_minutes=horizon,
                external_weather_record_id=(open_meteo_record.id if open_meteo_record and open_meteo_record.status in {"FRESH", "CACHED"} else None),
            )
            snapshot_id = int(assessment["snapshot_id"])
            if open_meteo_record is not None:
                external_service.mark_consumed(
                    open_meteo_record.id,
                    consumed_by="TOOL_01_AUTONOMOUS_MONITOR",
                    run_id=run.id,
                    snapshot_id=snapshot_id,
                )

            dynamic_pricing = None
            if bool(self.config.get("check_dynamic_pricing", True)):
                try:
                    dynamic_pricing = planner.analyze_dynamic_pricing(
                        snapshot_id=snapshot_id,
                        horizon_minutes=horizon,
                    )
                except Exception as exc:
                    dynamic_pricing = {"status": "UNAVAILABLE", "reason": str(exc)}

            events = detect_events(assessment, dynamic_pricing)
            trigger_cfg = self.config.get("triggers") or {}
            trigger_keys = {
                "CURRENT_IMBALANCE": "imbalance",
                "LOW_RESERVE": "low_reserve",
                "HIGH_FUTURE_RISK": "future_risk",
                "CRITICAL_FUTURE_RISK": "future_risk",
                "SOLAR_SURPLUS": "solar_surplus",
                "DYNAMIC_PRICING_ACTION_REQUIRED": "dynamic_pricing",
            }
            events = [
                item for item in events
                if trigger_cfg.get(trigger_keys.get(item.event_type, item.event_type.lower()), True) is not False
            ]
            primary = events[0] if events else None
            tick = MonitoringTick(
                run_id=run.id,
                snapshot_id=snapshot_id,
                condition=_snapshot_condition(snapshot_id),
                event_type=primary.event_type if primary else None,
                external_source_ids=external_ids,
                details_json={"detected_events": [event.to_dict() for event in events]},
            )
            db.session.add(tick)
            db.session.flush()

            if primary is None:
                db.session.commit()
                self._set_run_context(run, None, snapshot_id, events, external_ids)
                memory.close_agent_run_trace(run.id, "COMPLETED", "Scheduled monitor completed; no actionable event detected.")
                return {"status": "NO_EVENT", "run_id": run.id, "snapshot_id": snapshot_id, "tick_id": tick.id}

            signature = state_signature(snapshot_id, assessment, primary)
            duplicate = self._find_recent_duplicate(primary, signature)
            if duplicate is not None:
                tick.event_suppressed = True
                tick.event_id = duplicate.id
                db.session.commit()
                self._set_run_context(run, duplicate, snapshot_id, events, external_ids, suppressed=True)
                memory.close_agent_run_trace(run.id, "COMPLETED", f"Event {primary.event_type} suppressed by cooldown de-duplication.")
                return {
                    "status": "EVENT_SUPPRESSED", "event_id": duplicate.id, "event_type": primary.event_type,
                    "run_id": run.id, "snapshot_id": snapshot_id, "tick_id": tick.id,
                }

            event = OperationalEvent(
                event_type=primary.event_type,
                trigger_source=trigger_source,
                occurred_at=_utcnow_naive(),
                snapshot_id=snapshot_id,
                run_id=run.id,
                reason=primary.reason,
                severity=primary.severity,
                state_signature=signature,
                status="DETECTED",
                details_json={"primary": primary.to_dict(), "all_events": [item.to_dict() for item in events]},
            )
            db.session.add(event)
            db.session.flush()
            tick.event_id = event.id
            db.session.commit()
            self._set_run_context(run, event, snapshot_id, events, external_ids)

            planning = self._plan_for_event(planner, assessment, dynamic_pricing, primary, snapshot_id, horizon)
            selected_plan_id = planning.get("selected_plan_id") if isinstance(planning, dict) else None
            event.selected_plan_id = int(selected_plan_id) if selected_plan_id is not None else None
            if selected_plan_id is not None:
                event.status = "AWAITING_HUMAN_APPROVAL"
                summary = f"{primary.event_type} detected; plans generated and validated. Plan #{selected_plan_id} awaits Human Approval."
            else:
                event.status = "ASSESSED_NO_SELECTED_PLAN"
                summary = f"{primary.event_type} detected and assessed; no selected feasible plan is awaiting approval."
            db.session.commit()
            memory.close_agent_run_trace(run.id, "COMPLETED", summary)
            return {
                "status": event.status,
                "event_id": event.id,
                "event_type": event.event_type,
                "run_id": run.id,
                "snapshot_id": snapshot_id,
                "tick_id": tick.id,
                "selected_plan_id": event.selected_plan_id,
                "planning": planning,
            }
        except Exception as exc:
            db.session.rollback()
            try:
                memory.close_agent_run_trace(run.id, "FAILED", f"Autonomous monitor failed safely: {exc}")
            except Exception:
                db.session.rollback()
            raise

    def _plan_for_event(self, planner: PlannerAgent, assessment: dict[str, Any], dynamic_pricing: dict[str, Any] | None,
                        event: DetectedEvent, snapshot_id: int, horizon: int) -> dict[str, Any]:
        reserve_id = _read(assessment.get("reserve"), "assessment_id")
        risk_event_id = _read(assessment.get("future_risk"), "risk_event_id")
        if event.event_type == "DYNAMIC_PRICING_ACTION_REQUIRED":
            return planner.build_and_validate_dynamic_pricing_plans(
                snapshot_id=snapshot_id,
                horizon_minutes=horizon,
                risk_event_id=risk_event_id,
                reserve_assessment_id=reserve_id,
                objectives=["restore secure solar-only operation", "use deterministic dynamic pricing when feasible"],
            )
        return planner.build_and_validate_plans(
            snapshot_id=snapshot_id,
            horizon_minutes=horizon,
            risk_event_id=risk_event_id,
            reserve_assessment_id=reserve_id,
            objectives=["restore secure solar-only operation", "preserve reserve and network constraints"],
        )

    def _find_recent_duplicate(self, event: DetectedEvent, signature: str) -> OperationalEvent | None:
        cooldown = max(0, int(self.config.get("event_cooldown_seconds", 900)))
        cutoff = _utcnow_naive() - timedelta(seconds=cooldown)
        return (
            OperationalEvent.query
            .filter(OperationalEvent.event_type == event.event_type)
            .filter(OperationalEvent.state_signature == signature)
            .filter(OperationalEvent.occurred_at >= cutoff)
            .order_by(OperationalEvent.id.desc())
            .first()
        )

    @staticmethod
    def _set_run_context(run: AgentRunTrace, event: OperationalEvent | None, snapshot_id: int,
                         events: list[DetectedEvent], external_ids: list[int], suppressed: bool = False) -> None:
        run.decisions = {
            "trigger_context": {
                "trigger_type": event.event_type if event else "SCHEDULED_CHECK",
                "trigger_source": run.trigger_source,
                "trigger_timestamp": _utcnow_naive().isoformat(),
                "snapshot_id": snapshot_id,
                "event_id": event.id if event else None,
                "event_type": event.event_type if event else None,
                "reason": event.reason if event else "Scheduled monitor cycle",
                "planner_run_id": run.id,
                "suppressed_by_cooldown": suppressed,
            },
            "detected_events": [item.to_dict() for item in events],
            "external_data_provenance_ids": external_ids,
            "human_approval_required": True,
            "automatic_execution_allowed": False,
        }
        db.session.commit()

    def status(self) -> dict[str, Any]:
        with self.app.app_context():
            tick = MonitoringTick.query.order_by(MonitoringTick.id.desc()).first()
            event = OperationalEvent.query.order_by(OperationalEvent.id.desc()).first()
            latest_plan = Plan.query.order_by(Plan.id.desc()).first()
            interval = max(60, int(self.config.get("poll_interval_seconds", 300)))
            next_check = None
            if self._scheduler is not None:
                job = self._scheduler.get_job("solargrid-autonomous-monitor")
                next_run = getattr(job, "next_run_time", None) if job else None
                next_check = next_run.isoformat() if next_run else None
            return {
                "running": self._scheduler is not None and self._scheduler.running,
                "mode": "scheduled_event_driven" if self.enabled else "disabled",
                "poll_interval_seconds": interval,
                "event_cooldown_seconds": int(self.config.get("event_cooldown_seconds", 900)),
                "last_check": tick.checked_at.isoformat() if tick else None,
                "last_snapshot_id": tick.snapshot_id if tick else None,
                "last_condition": tick.condition if tick else "NO_CHECK_YET",
                "latest_event": _event_dict(event) if event else None,
                "latest_plan_id": latest_plan.id if latest_plan else None,
                "next_check": next_check,
                "last_error": self.last_error,
                "human_approval_required": True,
                "automatic_execution": False,
            }


def _snapshot_condition(snapshot_id: int) -> str:
    snapshot = db.session.get(SystemSnapshot, snapshot_id)
    return str(snapshot.grid_status if snapshot else "UNKNOWN")


def _event_dict(event: OperationalEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "event_type": event.event_type,
        "trigger_source": event.trigger_source,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "snapshot_id": event.snapshot_id,
        "run_id": event.run_id,
        "reason": event.reason,
        "severity": event.severity,
        "status": event.status,
        "selected_plan_id": event.selected_plan_id,
        "details": event.details_json or {},
    }


_service: SolarGridMonitorService | None = None


def get_monitor_service(app=None) -> SolarGridMonitorService | None:
    global _service
    if app is not None and (_service is None or _service.app is not app):
        _service = SolarGridMonitorService(app)
    return _service
