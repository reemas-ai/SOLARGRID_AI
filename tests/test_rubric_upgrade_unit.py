from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from automation.event_detector import detect_events, state_signature
from external_data.quality import freshness_status, plausible_range, required_fields

ROOT = Path(__file__).resolve().parents[1]


def _assessment(*, reserve="SUFFICIENT", imbalance="LOW", direction="BALANCED", risk="LOW", imbalance_mw=0.0):
    return {
        "snapshot_id": 1,
        "reserve": {"data": {"reserve_status": reserve, "required_reserve_mw": 10.0, "actual_reserve_mw": 15.0, "reserve_margin_mw": 5.0, "assessment_id": 7}},
        "imbalance": {"data": {"severity": imbalance, "direction": direction, "imbalance_mw": imbalance_mw, "event_required": imbalance in {"HIGH", "CRITICAL"}}},
        "future_risk": {"data": {"max_severity": risk, "risk_event_id": 9}},
    }


def test_low_reserve_event_is_detected():
    events = detect_events(_assessment(reserve="INSUFFICIENT"))
    assert any(event.event_type == "LOW_RESERVE" for event in events)


def test_dynamic_pricing_action_is_prioritized_over_plain_surplus():
    assessment = _assessment(imbalance="MEDIUM", direction="SURPLUS", imbalance_mw=12.5)
    dp = {"data": {"action_required": True, "total_shifted_mw": 8.0, "max_severity": "HIGH"}, "tool_call_id": 17}
    events = detect_events(assessment, dp)
    assert events[0].event_type == "DYNAMIC_PRICING_ACTION_REQUIRED"
    assert any(event.event_type == "SOLAR_SURPLUS" for event in events)


def test_high_future_risk_event_is_detected():
    events = detect_events(_assessment(risk="HIGH"))
    assert any(event.event_type == "HIGH_FUTURE_RISK" for event in events)


def test_material_state_signature_ignores_snapshot_and_record_ids():
    a = _assessment(reserve="INSUFFICIENT")
    b = _assessment(reserve="INSUFFICIENT")
    b["snapshot_id"] = 999
    b["reserve"]["data"]["assessment_id"] = 12345
    event_a = detect_events(a)[0]
    event_b = detect_events(b)[0]
    assert state_signature(1, a, event_a) == state_signature(999, b, event_b)


def test_external_freshness_and_quality_helpers():
    now = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)
    assert freshness_status(now - timedelta(minutes=5), max_age_minutes=30, now=now) == "FRESH"
    assert freshness_status(now - timedelta(minutes=45), max_age_minutes=30, now=now) == "STALE"
    ok, missing = required_fields({"a": 1, "b": None}, ["a", "b"])
    assert ok is False and missing == ["b"]
    assert plausible_range(50, 0, 100) is True
    assert plausible_range(150, 0, 100) is False


def test_automation_config_preserves_human_control():
    config = json.loads((ROOT / "config" / "automation_v1.json").read_text())
    assert config["enabled"] is True
    assert config["safety_contract"]["human_approval_required"] is True
    assert config["safety_contract"]["automatic_execution"] is False
    assert config["event_cooldown_seconds"] >= 900
    # A four-candidate autonomous cycle uses up to 20 deterministic tool calls:
    # Tools 01-04 (4) + Tool 17 precheck (1) + Tool 17 trusted refresh (1) +
    # Tool 05 (1) + Tools 06-08 for four candidates (12) + Tool 09 (1).
    assert config["planner_limits"]["max_tool_calls"] >= 20
    assert config["planner_limits"]["max_steps"] >= config["planner_limits"]["max_tool_calls"]


def test_external_config_has_all_three_sources_and_no_embedded_eia_key():
    config = json.loads((ROOT / "config" / "external_data_v1.json").read_text())
    assert config["open_meteo"]["requires_api_key"] is False
    assert config["nasa_power"]["requires_api_key"] is False
    assert config["eia"]["api_key_env"] == "EIA_API_KEY"
    assert "api_key" not in config["eia"]
    assert config["eia"]["route"].endswith("/data")


def test_ui_contains_provenance_and_status_semantics_labels():
    html = (ROOT / "templates" / "index.html").read_text()
    js = (ROOT / "static" / "js" / "main.js").read_text()
    assert "External Data & Trust" in html
    assert "SYNTHETIC DEMO" in html
    assert "Engineering Evidence Used" in js
    assert "Command Execution Quality" in js
    assert "Operational Goal" in js
