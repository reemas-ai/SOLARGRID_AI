"""Static regression for the browser/API reachability hardening.

This intentionally uses only the standard library so it can run even before
Flask/pandapower are installed. It verifies the source contract that prevents
known local-demo `Failed to fetch` failure modes.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
app = (ROOT / "app.py").read_text(encoding="utf-8")
api = (ROOT / "static/js/api.js").read_text(encoding="utf-8")
main = (ROOT / "static/js/main.js").read_text(encoding="utf-8")
html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
loader = (ROOT / "scripts/load_demo_dataset.py").read_text(encoding="utf-8")

checks = {
    "RAG prewarm is asynchronous": "threading.Thread(" in app and 'name="solargrid-rag-prewarm"' in app,
    "Flask development server is threaded": "threaded=True" in app,
    "Flask reloader is disabled": "use_reloader=False" in app,
    "API responses disable stale cache": 'Cache-Control' in app and 'no-store' in app,
    "dataset loader skips RAG prewarm": 'os.environ["SOLARGRID_RAG_PREWARM"] = "false"' in loader,
    "GET requests have transient network retries": "GET_NETWORK_RETRIES = 2" in api,
    "POST calls are not auto-replayed": "method==='GET'?GET_NETWORK_RETRIES:0" in api,
    "network errors have an operator-readable message": "SolarGrid API is temporarily unreachable" in api,
    "Execute allows long backend work": "timeoutMs:180000" in main,
    "Plan generation allows first-run solver warmup": "timeoutMs:120000" in main,
    "Assessment has an explicit timeout": "timeoutMs:90000" in main,
    "frontend module is cache-busted": "main.js') }}?v=4.0" in html,
}

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
if failed:
    raise SystemExit("Fetch/runtime contract failed: " + "; ".join(failed))
print("PASS: frontend/API runtime hardening contract is present.")
