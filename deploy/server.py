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
import ui

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
_proc: subprocess.Popen | None = None  # the running `applypilot run`, if any
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


def requeue_failed_scores() -> None:
    """Send jobs whose scoring failed back for scoring.

    Real scores are 1-10; the scorer stores 0 when the model's answer couldn't be
    read (overloaded model, empty answer), and those jobs are otherwise never retried.
    """
    import sqlite3
    db = DATA_DIR / "applypilot.db"
    if not db.exists():
        return
    try:
        conn = sqlite3.connect(db, timeout=10)
        n = conn.execute("UPDATE jobs SET fit_score = NULL, scored_at = NULL WHERE fit_score = 0").rowcount
        # Scoring instructions changed since these were scored: score again (keeps jobs that
        # already have a tailored resume).
        from applypilot.scoring.scorer import SCORE_PROMPT_VERSION
        marker = DATA_DIR / ".score_prompt_version"
        if not marker.exists() or marker.read_text().strip() != str(SCORE_PROMPT_VERSION):
            m = conn.execute("UPDATE jobs SET fit_score = NULL, scored_at = NULL "
                             "WHERE fit_score IS NOT NULL AND tailored_resume_path IS NULL").rowcount
            marker.write_text(str(SCORE_PROMPT_VERSION))
            log(f"Scoring instructions updated; re-scoring {m} job(s)")
        conn.commit()
        conn.close()
        if n:
            log(f"Re-queued {n} job(s) whose scoring failed")
    except sqlite3.Error as e:
        log(f"Could not re-queue failed scores: {e}")


def run_pipeline(wait: bool = False) -> None:
    global _proc
    if not _run_lock.acquire(blocking=wait, timeout=120 if wait else -1):
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

        requeue_failed_scores()
        args = list(RUN_ARGS)
        if "--min-score" not in args:
            args += ["--min-score", setup_form.saved_min_score(DATA_DIR)]
        cmd = [sys.executable, "-m", "applypilot", "run", *args]
        log(f"Starting pipeline: {shlex.join(cmd)}")
        with _state_lock:
            state["running"] = True
            state["last_started"] = now_iso()
        _proc = subprocess.Popen(cmd)
        code = _proc.wait()
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


def restart_pipeline() -> None:
    """Stop the search in progress (if any) and start a fresh one with the current settings."""
    proc = _proc
    if proc and proc.poll() is None:
        log("Restarting the search to apply new settings")
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    threading.Thread(target=run_pipeline, kwargs={"wait": True}, daemon=True).start()


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

def page(title: str, body: str, active: str = "", head: str = "") -> bytes:
    return ui.shell(title, body, active, extra_css=setup_form.FORM_CSS, head=head)


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
        "good_fit": f"SELECT COUNT(*) FROM jobs WHERE fit_score >= {int(setup_form.saved_min_score(DATA_DIR))}",
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
        return page("ApplyPilot status", f"""<h1>Almost ready</h1>
<p class="sub">ApplyPilot needs a few things before it can start searching.</p>
<div class="card"><ul style="margin:0 0 16px;padding-left:20px">{problems}</ul>
<a class="btn primary" href="/setup">Finish setup</a></div>""", "status")

    refresh = ""
    if s["running"]:
        dot, refresh = "run", '<meta http-equiv="refresh" content="60">'
        headline = "Searching for jobs"
        detail = (f"Started {_ago(s['last_started'])}. Jobs are scored while the search continues, and a full "
                  "run takes 30&ndash;90 minutes. This page refreshes itself every minute.")
    elif s["last_finished"]:
        ok = s["last_exit_code"] == 0
        dot = "ok" if ok else "warn"
        headline = "Search complete" if ok else "Search finished with some errors"
        detail = f"Finished {_ago(s['last_finished'])}."
    else:
        dot, headline, detail = "", "Waiting to start", ""
    nxt = _until(s["next_run"]) if s["next_run"] else "&mdash;"

    c = job_counts() or dict.fromkeys(("found", "described", "scored", "good_fit", "tailored", "covers"), 0)
    tiles = "".join(
        f'<div class="tile{cls}"><b>{val}</b><span>{label}</span></div>'
        for val, label, cls in (
            (c["found"], "Jobs found", ""),
            (c["scored"], "Scored for fit", ""),
            (c["good_fit"], f"Good fits ({setup_form.saved_min_score(DATA_DIR)}+)", " hl"),
            (c["tailored"], "Resumes written", ""),
            (c["covers"], "Cover letters", ""),
            (nxt.replace("in about ", ""), "Next search", " txt"),
        )
    )
    ready_label = f"Ready to apply ({c['tailored']})" if c["tailored"] else "Ready to apply"
    body = f"""<h1>Status</h1>
<p class="sub">ApplyPilot searches, scores and writes your applications automatically every 12 hours.</p>
<div class="card status"><span class="dot {dot}"></span><div><div class="head">{headline}</div>
<p class="muted small">{detail}</p></div></div>
<div class="tiles">{tiles}</div>
<div class="row"><a class="btn good" href="/ready">{ready_label}</a>
<a class="btn ghost" href="/">Browse all jobs</a></div>

<details class="card" style="margin-top:22px"{" open" if not c["tailored"] else ""}>
<summary><b>How it works</b></summary>
<ol class="steps" style="margin-top:16px">
<li><b>It searches for you.</b> <span class="muted">Every 12 hours it finds jobs matching your titles,
 scores each one 1&ndash;10 against your resume, and writes a tailored resume and cover letter for every job
 scoring {setup_form.saved_min_score(DATA_DIR)} or more (you can change this in Setup). Nothing to press.</span></li>
<li><b>Open Ready to apply.</b> <span class="muted">Your best matches, highest score first, each with its own
 resume and cover letter.</span></li>
<li><b>Download the resume.</b> <span class="muted">Tap <b>Resume</b> and read it over. Fix anything that
 isn't quite right.</span></li>
<li><b>Apply on the company's site.</b> <span class="muted">Tap <b>Apply</b> to open the real posting and
 upload that resume.</span></li>
<li><b>Cover letter, if asked.</b> <span class="muted">Tap <b>Cover letter</b>, then paste or upload it.</span></li>
</ol></details>

<details class="card"><summary class="muted">More options</summary>
<p class="muted small">Searches start on their own. Use this only to start one right now; it does nothing while a
search is already running.</p>
<form method="post" action="/run"><button class="btn ghost sm">Start a search now</button></form>
<p class="muted small" style="margin-bottom:0">All files: <a href="/files/tailored_resumes/">tailored resumes</a>
 &middot; <a href="/files/cover_letters/">cover letters</a></p></details>"""
    return page("ApplyPilot status", body, "status", head=refresh)


def _doc_href(path_str: str | None) -> str | None:
    """Download link for a generated document, preferring its PDF."""
    if not path_str:
        return None
    path = Path(path_str)
    if path.with_suffix(".pdf").exists():
        path = path.with_suffix(".pdf")
    elif not path.exists():
        return None
    try:
        return "/" + path.resolve().relative_to(DATA_DIR.resolve()).as_posix()
    except ValueError:
        return None


def ready_html() -> bytes:
    import sqlite3
    db = DATA_DIR / "applypilot.db"
    jobs = []
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            jobs = conn.execute(
                "SELECT title, site, location, url, application_url, fit_score, tailored_resume_path, "
                "cover_letter_path FROM jobs WHERE tailored_resume_path IS NOT NULL "
                "ORDER BY fit_score DESC, tailored_at DESC").fetchall()
            conn.close()
        except sqlite3.Error as e:
            log(f"Ready page query failed: {e}")
    if not jobs:
        c = job_counts() or {"good_fit": 0}
        note = (f"{c['good_fit']} good fits found so far. Their resumes are being written now."
                if c["good_fit"] else f"Jobs scoring {setup_form.saved_min_score(DATA_DIR)} or more will show up here "
                "with their own resume and cover letter.")
        body = f"""<h1>Ready to apply</h1>
<div class="card empty"><div class="big">📝</div><h2>Nothing ready yet</h2>
<p class="muted">{note}</p><a class="btn ghost" href="/status">Check progress</a></div>"""
        return page("Ready to apply", body, "ready")

    cards = []
    for j in jobs:
        score = j["fit_score"] or 0
        apply_url = j["application_url"] or j["url"] or ""
        meta = " &middot; ".join(html.escape(x) for x in (j["site"], j["location"]) if x)
        actions = []
        if href := _doc_href(j["tailored_resume_path"]):
            actions.append(f'<a class="btn good sm" href="{html.escape(href)}" target="_blank">Resume</a>')
        if href := _doc_href(j["cover_letter_path"]):
            actions.append(f'<a class="btn ghost sm" href="{html.escape(href)}" target="_blank">Cover letter</a>')
        if apply_url:
            actions.append(f'<a class="btn primary sm" href="{html.escape(apply_url)}" target="_blank" '
                           f'rel="noopener">Apply &rarr;</a>')
        cards.append(f"""<div class="card job"><div class="job-top">
<div class="score{'' if score >= 7 else ' mid'}">{score}</div><div>
<a class="job-title" href="{html.escape(j['url'] or apply_url)}" target="_blank" rel="noopener">
{html.escape(j['title'] or 'Untitled')}</a><div class="meta">{meta}</div></div></div>
<div class="row" style="gap:8px">{''.join(actions)}</div></div>""")
    body = f"""<h1>Ready to apply</h1>
<p class="sub">{len(jobs)} job{'s' if len(jobs) != 1 else ''} with a tailored resume, best match first. Download the
resume, then tap Apply.</p>
<div class="stack">{''.join(cards)}</div>"""
    return page("Ready to apply", body, "ready")


def listing_html(rel: str, directory: Path) -> bytes:
    items = sorted((p for p in directory.iterdir() if p.is_file() and p.suffix in (".pdf", ".txt")
                    and not p.stem.endswith("_JOB")), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = "".join(
        f'<a href="/files/{rel}/{html.escape(p.name)}" target="_blank">{html.escape(p.stem.replace("_", " "))}'
        f'<span>{p.suffix[1:].upper()}</span></a>' for p in items
    )
    title = rel.replace("_", " ").capitalize()
    body = (f'<h1>{title}</h1><p class="sub">Every file generated so far. '
            f'<a href="/ready">Ready to apply</a> shows them matched to their jobs.</p>'
            f'<div class="card files">{rows or "<p class=muted>Nothing here yet.</p>"}</div>')
    return page(title, body)


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
            return self._send(200, page("ApplyPilot setup", setup_form.render(DATA_DIR, os.environ), "setup"))
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
                return self._send(200, ui.dashboard_with_topbar(dash.read_text(encoding="utf-8")).encode())
            return self._send(200, status_html())
        if path == "/status":
            return self._send(200, status_html())
        if path == "/ready":
            return self._send(200, ready_html())
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
                                        setup_form.render(DATA_DIR, os.environ, fields, errors), "setup"))
        log("Setup saved from the web form")
        with _state_lock:
            state["config_problems"] = seed_config()
        restart_pipeline()
        self._send(200, page("ApplyPilot setup", setup_form.render(DATA_DIR, os.environ, saved=True), "setup"))


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
