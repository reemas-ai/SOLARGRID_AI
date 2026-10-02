"""Dependency-free SolarGrid preflight for the supported Python 3.12 runtime."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    raise SystemExit(1)


def main() -> None:
    if sys.version_info[:2] != (3, 12):
        fail(f"SolarGrid checkpoint targets Python 3.12.x; current interpreter is {sys.version.split()[0]}")

    py_files = [p for p in ROOT.rglob("*.py") if ".venv" not in p.parts]
    syntax_errors = []
    for path in py_files:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 12))
        except Exception as exc:
            syntax_errors.append(f"{path.relative_to(ROOT)}: {exc}")
    if syntax_errors:
        fail("Python 3.12 syntax check failed:\n  " + "\n  ".join(syntax_errors))

    required = [
        "app.py", "database.py", "schemas.py", "tool_registry.py",
        "agents/planner_agent.py", "agents/safety_agent.py",
        "tools/state_tools.py", "tools/planning_tools.py", "tools/operational_tools.py",
        "datasets/normal.json", "datasets/problem.json",
    ]
    missing = [item for item in required if not (ROOT / item).exists()]
    if missing:
        fail("Missing required project files: " + ", ".join(missing))

    for dataset_name in ("normal.json", "problem.json"):
        path = ROOT / "datasets" / dataset_name
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            fail(f"Invalid JSON in datasets/{dataset_name}: {exc}")

    print(f"OK: Python {sys.version.split()[0]}")
    print(f"OK: {len(py_files)} Python files parse with the Python 3.12 grammar")
    print("OK: required project files are present")
    print("OK: normal/problem dataset JSON is valid")
    print("NEXT: python -m pip install -r requirements.txt")
    print("NEXT: python scripts/load_demo_dataset.py normal")
    print("NEXT: python app.py")


if __name__ == "__main__":
    main()
