"""Deterministic configuration loader for SOLARGRID AI Team 2."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


def load_json(name: str) -> dict[str, Any]:
    path = ROOT / "config" / name
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_decision_policy(version: str | None = None) -> dict[str, Any]:
    policy = load_json("decision_policy_v1.json")
    if version is not None and policy.get("policy_version") != version:
        raise ValueError(f"Unsupported decision policy version: {version}")
    return policy


def load_generator_seed() -> dict[str, Any]:
    return load_json("generators_seed.json")


def load_battery_seed() -> dict[str, Any]:
    return load_json("batteries_seed.json")


def load_dynamic_pricing_config(version: str | None = None) -> dict[str, Any]:
    """Load the Solar-only Dynamic Pricing feature configuration."""
    config = load_json("dynamic_pricing_v1.json")
    if config.get("energy_scope") != "SOLAR_ONLY":
        raise ValueError("Dynamic Pricing configuration must remain SOLAR_ONLY")
    if version is not None and config.get("profile_version") != version:
        raise ValueError(f"Unsupported Dynamic Pricing config version: {version}")
    return config


def load_fixture() -> dict[str, Any]:
    path = ROOT / "fixtures" / "golden_fixture.json"
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
