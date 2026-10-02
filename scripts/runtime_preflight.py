"""Local runtime preflight for SolarGrid on Windows/Python 3.12.

Run before ``python app.py`` when the browser says the API is unreachable.
It does not change the database or start the server.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_MODULES = {
    "flask": "Flask",
    "flask_cors": "Flask-CORS",
    "flask_sqlalchemy": "Flask-SQLAlchemy",
    "sqlalchemy": "SQLAlchemy",
    "dotenv": "python-dotenv",
    "pydantic": "pydantic",
    "numpy": "numpy",
    "scipy": "scipy",
    "pandas": "pandas",
    "pandapower": "pandapower",
    "requests": "requests",
    "pypdf": "pypdf",
    "apscheduler": "APScheduler",
}


def main() -> int:
    errors: list[str] = []
    print(f"Python: {sys.version.split()[0]}")
    if sys.version_info[:2] != (3, 12):
        errors.append("SolarGrid target runtime is Python 3.12.x")

    for module, package in REQUIRED_MODULES.items():
        if importlib.util.find_spec(module) is None:
            errors.append(f"Missing package: {package} (import {module})")

    for rel in [
        "app.py", "database.py", "datasets/normal.json", "datasets/problem.json",
        "config/decision_policy_v1.json", "config/network_benchmark_v1.json",
        "templates/index.html", "static/js/main.js", "static/js/api.js",
        "automation/monitor_service.py", "automation/event_detector.py",
        "external_data/service.py", "config/automation_v1.json",
        "config/external_data_v1.json",
    ]:
        if not (ROOT / rel).exists():
            errors.append(f"Missing project file: {rel}")

    for rel in ["datasets/normal.json", "datasets/problem.json", "datasets/dynamic_pricing.json",
                "config/automation_v1.json", "config/external_data_v1.json"]:
        try:
            json.loads((ROOT / rel).read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"Invalid JSON in {rel}: {exc}")

    # Port test is advisory: an already-running SolarGrid server legitimately owns it.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.35)
    try:
        occupied = sock.connect_ex(("127.0.0.1", 5000)) == 0
    finally:
        sock.close()
    print(f"Port 5000: {'already in use (server may already be running)' if occupied else 'available'}")

    if errors:
        print("\nPRECHECK FAILED")
        for item in errors:
            print(" -", item)
        print("\nInstall the project requirements with Python 3.12, then retry:")
        print("  py -3.12 -m pip install -r requirements.txt")
        return 1

    print("PRECHECK PASS: required runtime modules and project files are available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
