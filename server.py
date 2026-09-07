"""Local control server for the scraper dashboard.

Serves the dashboard + progress.json from output/, and exposes:
  POST /api/start  - launches (or resumes) the scraper as a subprocess
  POST /api/stop   - asks the running scraper to pause after its current site
  GET  /api/status - {running: bool}

Run with: python server.py   (then open http://localhost:8765/dashboard.html)
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
PROGRESS_PATH = os.path.join(OUTPUT_DIR, "progress.json")
STOP_FLAG_PATH = os.path.join(OUTPUT_DIR, "stop.flag")
PORT = 8765
STALE_AFTER_SECONDS = 20  # if progress.json hasn't updated in this long, treat the run as dead

current_proc: subprocess.Popen | None = None


def is_running() -> bool:
    """True if we hold a live subprocess handle, OR if progress.json says
    'running' and was updated recently — covers the case where this server
    restarted while a scrape it didn't launch is still going (e.g. a prior
    server instance was killed but its child process survived)."""
    if current_proc is not None and current_proc.poll() is None:
        return True
    try:
        with open(PROGRESS_PATH, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("status") != "running":
            return False
        updated = datetime.fromisoformat(state["updated_at"])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        return age < STALE_AFTER_SECONDS
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return False


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            return self._json(200, {"running": is_running()})
        # serve static files (dashboard.html, progress.json, ...) from output/
        rel = self.path.split("?")[0].lstrip("/") or "dashboard.html"
        file_path = os.path.join(OUTPUT_DIR, rel)
        if not os.path.abspath(file_path).startswith(OUTPUT_DIR) or not os.path.isfile(file_path):
            return self._json(404, {"error": "not found"})
        content_type = "text/html" if file_path.endswith(".html") else \
            "application/json" if file_path.endswith(".json") else \
            "text/csv" if file_path.endswith(".csv") else "application/octet-stream"
        with open(file_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global current_proc

        if self.path.startswith("/api/start"):
            if is_running():
                return self._json(200, {"status": "already_running"})
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            engine = params.get("engine", "free")
            if engine not in ("free", "gemini"):
                engine = "free"
            current_proc = subprocess.Popen(
                [sys.executable, "main.py", "--resume", "--engine", engine],
                cwd=BASE_DIR,
            )
            return self._json(200, {"status": "started"})

        if self.path == "/api/stop":
            if not is_running():
                return self._json(200, {"status": "not_running"})
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            with open(STOP_FLAG_PATH, "w") as f:
                f.write("stop")
            return self._json(200, {"status": "stopping"})

        return self._json(404, {"error": "unknown endpoint"})

    def log_message(self, fmt, *args):
        pass  # keep the console quiet


if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"Control server running at http://localhost:{PORT}/dashboard.html")
    server.serve_forever()
