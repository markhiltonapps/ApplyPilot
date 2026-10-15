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

import setup_form

DATA_DIR = Path(os.environ.setdefault("APPLYPILOT_DIR", "/data"))
PORT = int(os.environ.get("PORT", "8080"))
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
USER = os.environ.get("DASHBOARD_USER", "applypilot")
INTERVAL_HOURS = float(os.environ.get("RUN_INTERVAL_HOURS", "12"))
RUN_ON_START = os.environ.get("RUN_ON_START", "true").lower() in ("1", "true", "yes")
RUN_ARGS = shlex.split(os.environ.get("RUN_ARGS", "--validation normal --stream --workers 2"))

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
    setup_form.rebuild_from_saved(DATA_DIR)

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

    if not (DATA_DIR / "resume.txt").exists():
        problems.append("Resume not added yet (use the setup page)")
    if not (DATA_DIR / "profile.json").exists():
        problems.append("Profile not filled in yet (use the setup page)")
    if not setup_form.has_llm_key(DATA_DIR, os.environ):
        problems.append("Gemini API key not added yet (use the setup page)")
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

        cmd = [sys.executable, "-m", "applypilot", "run", *RUN_ARGS]
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
<style>:root{{--line:#343a46;--field:#1b1f27;--muted:#9aa3b2}}
body{{font-family:system-ui,sans-serif;max-width:720px;margin:1.5rem auto;padding:0 16px;
background:#0f1115;color:#e6e6e6;line-height:1.45}}a{{color:#7cb7ff}}code,pre{{background:#1b1f27;
padding:2px 6px;border-radius:4px}}pre{{padding:12px;white-space:pre-wrap;word-break:break-word}}
button{{padding:8px 14px}}table.counts{{width:100%;border-collapse:collapse;margin:12px 0}}
table.counts td{{padding:8px 4px;border-bottom:1px solid var(--line)}}table.counts td:last-child{{text-align:right}}
.hint{{color:var(--muted)}}.guide{{background:#171b22;border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:16px 0}}.guide li{{margin:8px 0}}.big{{display:block;text-align:center;padding:14px;border-radius:10px;
background:#2f6fed;color:#fff;font-weight:600;text-decoration:none;margin:12px 0}}
{setup_form.FORM_CSS}</style></head>
<body>{body}</body></html>""".encode()


def job_counts() -> dict[str, int] | None:
    """Counts of jobs at each pipeline stage, read straight from the job database."""
    import sqlite3
    db = DATA_DIR / "applypilot.db"
    if not db.exists():
        return None
    queries = {
        "found": "SELECT COUNT(*) FROM jobs",
        "described": "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL",
        "scored": "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL",
        "good_fit": "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7",
        "tailored": "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL",
        "covers": "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL",
    }
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            return {k: conn.execute(q).fetchone()[0] for k, q in queries.items()}
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _ago(iso: str | None) -> str:
    if not iso:
        return ""
    mins = int((datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    return f"{mins // 60} h {mins % 60} min ago"


def _until(iso: str | None) -> str:
    if not iso:
        return ""
    mins = int((datetime.fromisoformat(iso) - datetime.now(UTC)).total_seconds() // 60)
    return f"in about {mins // 60} h {mins % 60} min" if mins >= 60 else f"in about {max(mins, 1)} min"


def status_html() -> bytes:
    with _state_lock:
        s = dict(state)
    if s["config_problems"]:
        problems = "".join(f"<li>{html.escape(p)}</li>" for p in s["config_problems"])
        return page("ApplyPilot status", f"""<h1>ApplyPilot</h1><h3>Setup needed</h3><ul>{problems}</ul>
<a class="big" href="/setup">Open setup page</a>""")

    if s["running"]:
        headline = f"🔄 Searching for jobs. Started {_ago(s['last_started'])}."
        detail = ("A full run usually takes 30–90 minutes. This page doesn't update by itself: "
                  "refresh it to see the numbers go up.")
    elif s["last_finished"]:
        ok = s["last_exit_code"] == 0
        headline = (f"✅ Last search finished {_ago(s['last_finished'])}." if ok else
                    f"⚠️ Last search finished {_ago(s['last_finished'])} with some errors.")
        detail = f"Next automatic search {_until(s['next_run'])}." if s["next_run"] else ""
    else:
        headline = "Waiting to start."
        detail = f"Next automatic search {_until(s['next_run'])}." if s["next_run"] else ""

    c = job_counts()
    rows = ""
    if c:
        steps = [
            ("Jobs found", c["found"]),
            ("Full job descriptions fetched", c["described"]),
            ("Jobs scored against your resume", c["scored"]),
            ("Good fits (7+ out of 10)", c["good_fit"]),
            ("Tailored resumes written", c["tailored"]),
            ("Cover letters written", c["covers"]),
        ]
        rows = "<table class='counts'>" + "".join(
            f"<tr><td>{label}</td><td><b>{n}</b></td></tr>" for label, n in steps) + "</table>"

    body = f"""<h1>ApplyPilot</h1>
<p style="font-size:18px">{headline}</p>
<p class="hint">{detail}</p>
{rows}
<a class="big" href="/">See jobs found (dashboard)</a>
<p><a href="/files/tailored_resumes/">Tailored resumes</a> · <a href="/files/cover_letters/">Cover letters</a>
 · <a href="/setup">Edit setup</a></p>
<details class="guide"{" open" if not (c and c["tailored"]) else ""}><summary><b>How to use ApplyPilot</b></summary>
<ol>
<li><b>Wait for a search to finish.</b> Every 12 hours ApplyPilot finds new jobs, scores each one 1&ndash;10
 against your resume, then writes a tailored resume and a cover letter for every job scoring 7 or higher.
 You don't need to press anything.</li>
<li><b>Open the dashboard</b> with the blue button above. Tap <b>7+ Strong</b> to see only your best matches.
 Each card shows why it scored that way.</li>
<li><b>Pick a job</b> and tap the green <b>Resume</b> button to download the resume written for that job.
 Read it over and fix anything that isn't quite right.</li>
<li><b>Tap Apply</b> (or View job) to open the real job posting, and apply there using that resume.</li>
<li><b>Cover letter (optional):</b> if the application asks for one, tap the green <b>Cover letter</b>
 button, copy it, and paste or upload it.</li>
</ol>
<p class="hint">Green buttons only appear on jobs scoring 7+, once their resume has been written.
 To change the jobs it looks for, use <a href="/setup">Edit setup</a>.</p>
</details>
<details><summary class="hint">More options</summary>
<p class="hint">Searches run automatically every few hours. Use this only to start one right now;
it does nothing while a search is already running.</p>
<form method="post" action="/run"><button>Start a search now</button></form></details>"""
    return page("ApplyPilot status", body)


NAV_HTML = """<nav style="display:flex;gap:18px;flex-wrap:wrap;align-items:center;padding:10px 0 18px;
font-size:15px"><a href="/status" style="color:#60a5fa">← Status &amp; how-to</a>
<a href="/setup" style="color:#60a5fa">Edit setup</a>
<a href="/files/tailored_resumes/" style="color:#60a5fa">All tailored resumes</a>
<a href="/files/cover_letters/" style="color:#60a5fa">All cover letters</a></nav>"""


def add_nav(dashboard_html: str) -> str:
    """Put links back to the rest of the site at the top of the generated dashboard."""
    return dashboard_html.replace("<body>", "<body>" + NAV_HTML, 1)


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

        if path == "/setup":
            return self._send(200, page("ApplyPilot setup", setup_form.render(DATA_DIR, os.environ)))
        if path == "/":
            if seed_config():
                return self._send(303, b"", "text/plain", {"Location": "/setup"})
            dash = DATA_DIR / "dashboard.html"
            if (DATA_DIR / "applypilot.db").exists():
                try:  # rebuild so jobs found mid-run show up
                    from applypilot.view import generate_dashboard as _gen
                    _gen(str(dash))
                except Exception as e:  # noqa: BLE001 - fall back to the last generated dashboard
                    log(f"Dashboard refresh failed: {e}")
            if dash.exists():
                return self._send(200, add_nav(dash.read_text(encoding="utf-8")).encode())
            return self._send(200, status_html())
        if path == "/status":
            return self._send(200, status_html())
        if path == "/status.json":
            with _state_lock:
                return self._send(200, json.dumps(state).encode(), "application/json")

        if path.split("/")[1] in SERVED_DIRS:  # dashboard links are relative to the data dir
            path = "/files" + path
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
        path = urlparse(self.path).path
        if path not in ("/run", "/setup"):
            return self._send(404, b"Not found", "text/plain")
        if not self._authorized():
            return
        if path == "/setup":
            return self._post_setup()
        if _run_lock.locked():
            msg = "Pipeline is already running."
        else:
            threading.Thread(target=run_pipeline, daemon=True).start()
            msg = "Pipeline started."
        self._send(303, msg.encode(), "text/plain", {"Location": "/status"})

    def _post_setup(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > setup_form.MAX_UPLOAD_BYTES + 1024 * 1024:
            return self._send(413, page("Too large", "<p>That upload is too large (10 MB max).</p>"))
        body = self.rfile.read(length)
        fields, files = setup_form.parse_multipart(self.headers.get("Content-Type", ""), body)
        errors = setup_form.save(DATA_DIR, fields, files, os.environ)
        if errors:
            log(f"Setup form not complete: {errors}")
            return self._send(200, page("ApplyPilot setup",
                                        setup_form.render(DATA_DIR, os.environ, fields, errors)))
        log("Setup saved from the web form")
        with _state_lock:
            state["config_problems"] = seed_config()
        if not _run_lock.locked():
            threading.Thread(target=run_pipeline, daemon=True).start()
        self._send(200, page("ApplyPilot setup", setup_form.render(DATA_DIR, os.environ, saved=True)))


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
