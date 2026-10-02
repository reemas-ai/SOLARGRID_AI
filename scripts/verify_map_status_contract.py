"""Verify SolarGrid map status behavior for the shipped normal/problem datasets.

This test is independent of Flask so it can run in lightweight repair/build
containers. It verifies the exact tri-color contract used by the UI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from map_status import VALID_MAP_STATUSES, status_from_availability  # noqa: E402


def load_dataset(name: str) -> dict:
    return json.loads((ROOT / "datasets" / f"{name}.json").read_text(encoding="utf-8"))


def generator_colors(data: dict) -> list[str]:
    return [status_from_availability(row.get("availability_status"))[0] for row in data["generators"]]


def main() -> None:
    expected = {
        "normal": ["GREEN", "GREEN", "GREEN", "GREEN", "GREEN", "GREEN"],
        "problem": ["RED", "GREEN", "GREEN", "GREEN", "YELLOW", "GREEN"],
    }

    for name, expected_colors in expected.items():
        data = load_dataset(name)
        colors = generator_colors(data)
        assert colors == expected_colors, f"{name}: expected {expected_colors}, got {colors}"
        assert set(colors) <= VALID_MAP_STATUSES
        assert "GRAY" not in colors
        print(f"[{name}] generator markers: {', '.join(colors)}")

    assert "GRAY" not in VALID_MAP_STATUSES

    # Unknown/unrecognized engineering availability must never become gray.
    fallback, fallback_reasons = status_from_availability(None)
    assert fallback == "YELLOW"
    assert fallback_reasons
    print("[fallback] missing/unrecognized availability -> YELLOW (operator attention)")
    print("PASS: map exposes only GREEN / YELLOW / RED.")


if __name__ == "__main__":
    main()
