"""ApplyPilot server runner for container hosts (Railway, Fly, Docker).

Runs the ApplyPilot pipeline (discover → enrich → score → tailor → cover → pdf)
on a fixed interval and serves the generated HTML dashboard plus the tailored
resumes and cover letters over HTTP, behind HTTP basic auth.

Configuration (environment variables):
    APPLYPILOT_DIR            Data directory; mount a persistent volume here (default /data).
    APPLYPILOT_PROFILE_JSON   Contents of profile.json. Written to the data dir on every start.
    APPLYPILOT_RESUME_TXT     Contents of resume.txt (plain text). Written on every start.
    APPLYPILOT_SEARCHES_YAML  Contents of searches.yaml. Optional; the packaged example is used otherwise.
    GEMINI_API_KEY / OPENAI_API_KEY / LLM_URL / LLM_MODEL   LLM provider, as for the CLI.
    DASHBOARD_PASSWORD        Required to view anything except /health.
    DASHBOARD_USER            Basic-auth user name (default "applypilot").
    RUN_INTERVAL_HOURS        Hours between pipeline runs (default 12; 0 disables the schedule).
    RUN_ON_START              Run the pipeline right after boot (default "true").
    RUN_ARGS                  Extra arguments for `applypilot run` (default "--validation normal").
    PORT                      HTTP port (default 8080).
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import mimetypes
import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

DATA_DIR = Path(os.environ.setdefault("APPLYPILOT_DIR", "/data"))
PORT = int(os.environ.get("PORT", "8080"))
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
USER = os.environ.get("DASHBOARD_USER", "applypilot")
INTERVAL_HOURS = float(os.environ.get("RUN_INTERVAL_HOURS", "12"))
RUN_ON_START = os.environ.get("RUN_ON_START", "true").lower() in ("1", "true", "yes")
RUN_ARGS = shlex.split(os.environ.get("RUN_ARGS", "--validation normal"))

# Output folders that may be browsed and downloaded from the dashboard.
SERVED_DIRS = ("tailored_resumes", "cover_letters")

_state_lock = threading.Lock()
_run_lock = threading.Lock()
_wake = threading.Event()
state: dict = {
    "running": False,
    "last_started": None,
    "last_finished": None,
    "last_exit_code": None,
    "next_run": None,
    "config_problems": [],
}


def log(msg: str) -> None:
    print(f"[server] {datetime.now(UTC).isoformat(timespec='seconds')} {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Config seeding
# ---------------------------------------------------------------------------

def seed_config() -> list[str]:
    """Write profile/resume/searches from env vars into the data dir.

    Returns a list of problems that prevent the pipeline from running.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    profile = os.environ.get("APPLYPILOT_PROFILE_JSON", "").strip()
    if profile:
        try:
            json.loads(profile)
            (DATA_DIR / "profile.json").write_text(profile, encoding="utf-8")
        except json.JSONDecodeError as e:
            problems.append(f"APPLYPILOT_PROFILE_JSON is not valid JSON: {e}")

    resume = os.environ.get("APPLYPILOT_RESUME_TXT", "")
    if resume.strip():
        (DATA_DIR / "resume.txt").write_text(resume, encoding="utf-8")

    searches = os.environ.get("APPLYPILOT_SEARCHES_YAML", "")
    if searches.strip():
        import yaml
        try:
            yaml.safe_load(searches)
            (DATA_DIR / "searches.yaml").write_text(searches, encoding="utf-8")
        except yaml.YAMLError as e:
            problems.append(f"APPLYPILOT_SEARCHES_YAML is not valid YAML: {e}")

    if not (DATA_DIR / "profile.json").exists():
        problems.append("profile.json missing: set APPLYPILOT_PROFILE_JSON")
    if not (DATA_DIR / "resume.txt").exists():
        problems.append("resume.txt missing: set APPLYPILOT_RESUME_TXT")
    if not any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL")):
        problems.append("No LLM configured: set GEMINI_API_KEY (or OPENAI_API_KEY / LLM_URL)")
    return problems


# ---------------------------------------------------------------------------
# Pipeline scheduling
# ---------------------------------------------------------------------------

def generate_dashboard() -> None:
    subprocess.run(
        [sys.executable, "-c", "from applypilot.view import generate_dashboard; generate_dashboard()"],
        check=False,
    )


def run_pipeline() -> None:
    if not _run_lock.acquire(blocking=False):
        log("Pipeline already running; skipping.")
        return
    try:
        problems = seed_config()
        with _state_lock:
            state["config_problems"] = problems
        if problems:
            for p in problems:
                log(f"Not running pipeline: {p}")
            return

        cmd = ["applypilot", "run", *RUN_ARGS]
        log(f"Starting pipeline: {shlex.join(cmd)}")
        with _state_lock:
            state["running"] = True
            state["last_started"] = now_iso()
        code = subprocess.run(cmd, check=False).returncode
        log(f"Pipeline finished with exit code {code}")
        generate_dashboard()
        with _state_lock:
            state["running"] = False
            state["last_finished"] = now_iso()
            state["last_exit_code"] = code
    finally:
        with _state_lock:
            state["running"] = False
        _run_lock.release()


def scheduler() -> None:
    first = True
    while True:
        if not first or RUN_ON_START:
            run_pipeline()
        first = False
        if INTERVAL_HOURS <= 0:
            with _state_lock:
                state["next_run"] = None
            _wake.wait()
        else:
            delay = INTERVAL_HOURS * 3600
            with _state_lock:
                state["next_run"] = datetime.fromtimestamp(time.time() + delay, UTC).isoformat(
                    timespec="seconds")
            _wake.wait(delay)
        _wake.clear()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def page(title: str, body: str) -> bytes:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 16px;
background:#0f1115;color:#e6e6e6}}a{{color:#7cb7ff}}code,pre{{background:#1b1f27;padding:2px 6px;
border-radius:4px}}pre{{padding:12px;overflow:auto}}button{{padding:8px 14px}}</style></head>
<body>{body}</body></html>""".encode()


def status_html() -> bytes:
    with _state_lock:
        s = dict(state)
    problems = "".join(f"<li>{html.escape(p)}</li>" for p in s["config_problems"])
    links = " · ".join(f'<a href="/files/{d}/">{d}</a>' for d in SERVED_DIRS)
    body = f"""<h1>ApplyPilot</h1>
<p><a href="/">Dashboard</a> · {links}</p>
<pre>{html.escape(json.dumps(s, indent=2))}</pre>
{"<h3>Configuration problems</h3><ul>" + problems + "</ul>" if problems else ""}
<form method="post" action="/run"><button>Run pipeline now</button></form>"""
    return page("ApplyPilot status", body)


def listing_html(rel: str, directory: Path) -> bytes:
    items = sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = "".join(
        f'<li><a href="/files/{rel}/{html.escape(p.name)}">{html.escape(p.name)}</a></li>'
        for p in items if p.is_file()
    )
    return page(rel, f'<p><a href="/status">← status</a></p><h1>{rel}</h1><ul>{rows or "<li>(empty)</li>"}</ul>')


class Handler(BaseHTTPRequestHandler):
    server_version = "ApplyPilot"

    def log_message(self, fmt, *args):  # quieter access log
        if not self.path.startswith("/health"):
            sys.stderr.write(f"[http] {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not PASSWORD:
            self._send(503, page("Locked", "<h1>Dashboard locked</h1><p>Set <code>DASHBOARD_PASSWORD</code> "
                                           "to view results.</p>"))
            return False
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:  # bad base64 or non-UTF-8 credentials
                user, pw = "", ""
            if hmac.compare_digest(user, USER) and hmac.compare_digest(pw, PASSWORD):
                return True
        self._send(401, b"Authentication required", "text/plain",
                   {"WWW-Authenticate": 'Basic realm="ApplyPilot", charset="UTF-8"'})
        return False

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        if not self._authorized():
            return

        if path == "/":
            dash = DATA_DIR / "dashboard.html"
            if dash.exists():
                return self._send(200, dash.read_bytes())
            return self._send(200, status_html())
        if path == "/status":
            return self._send(200, status_html())
        if path == "/status.json":
            with _state_lock:
                return self._send(200, json.dumps(state).encode(), "application/json")

        if path.startswith("/files/"):
            rel = path[len("/files/"):].strip("/")
            top = rel.split("/", 1)[0]
            if top in SERVED_DIRS:
                root = (DATA_DIR / top).resolve()
                target = (DATA_DIR / rel).resolve()
                if target == root and root.is_dir():
                    return self._send(200, listing_html(top, root))
                if target.is_file() and target.is_relative_to(root):
                    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    if ctype.startswith("text/"):
                        ctype += "; charset=utf-8"
                    return self._send(200, target.read_bytes(), ctype)
        self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/run":
            return self._send(404, b"Not found", "text/plain")
        if not self._authorized():
            return
        if _run_lock.locked():
            msg = "Pipeline is already running."
        else:
            threading.Thread(target=run_pipeline, daemon=True).start()
            msg = "Pipeline started."
        self._send(303, msg.encode(), "text/plain", {"Location": "/status"})


def main() -> None:
    problems = seed_config()
    state["config_problems"] = problems
    for p in problems:
        log(f"Config: {p}")
    if PASSWORD:
        log(f"Dashboard enabled (user '{USER}')")
    else:
        log("DASHBOARD_PASSWORD not set: web UI is locked; only /health responds.")

    threading.Thread(target=scheduler, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Listening on :{PORT} (data dir {DATA_DIR}, interval {INTERVAL_HOURS}h)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
