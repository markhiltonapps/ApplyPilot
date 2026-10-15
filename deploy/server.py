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
from urllib.parse import parse_qs, unquote, urlparse

import editor
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
        # ...or the resume changed since then: every score is relative to the resume.
        import hashlib

        from applypilot.scoring.scorer import SCORE_PROMPT_VERSION
        resume = DATA_DIR / "resume.txt"
        resume_hash = hashlib.sha256(resume.read_bytes()).hexdigest()[:16] if resume.exists() else ""
        version = f"{SCORE_PROMPT_VERSION}:{resume_hash}"
        marker = DATA_DIR / ".score_prompt_version"
        old_version = marker.read_text().strip() if marker.exists() else ""
        # Old markers held only the prompt version; treat a matching one as current for this resume.
        if old_version == str(SCORE_PROMPT_VERSION):
            marker.write_text(version)
        elif old_version != version:
            m = conn.execute("UPDATE jobs SET fit_score = NULL, scored_at = NULL "
                             "WHERE fit_score IS NOT NULL AND tailored_resume_path IS NULL").rowcount
            marker.write_text(version)
            log(f"Resume or scoring instructions changed; re-scoring {m} job(s)")
        # One-time: tailoring attempts that failed because replies were cut off (output limit,
        # then OpenRouter's default reasoning) shouldn't count toward the 5-attempt cap.
        reset_marker = DATA_DIR / ".tailor_attempts_reset_v1"
        if not reset_marker.exists():
            r = conn.execute("UPDATE jobs SET tailor_attempts = 0 WHERE tailored_resume_path IS NULL "
                             "AND COALESCE(tailor_attempts, 0) > 0").rowcount
            reset_marker.write_text("1")
            log(f"Reset tailoring attempts for {r} job(s)")
        # One-time: rewrite every resume and cover letter made before roles were kept separate
        # with their exact dates (older ones merged the Compaq roles) and the email was updated.
        retailor_marker = DATA_DIR / ".retailor_v2"
        if not retailor_marker.exists():
            r = conn.execute(
                "UPDATE jobs SET tailored_resume_path = NULL, tailored_at = NULL, tailor_attempts = 0, "
                "cover_letter_path = NULL, cover_letter_at = NULL, cover_attempts = 0 "
                "WHERE tailored_resume_path IS NOT NULL OR cover_letter_path IS NOT NULL").rowcount
            retailor_marker.write_text("1")
            log(f"Rewriting resumes and cover letters for {r} job(s)")
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
    return ui.shell(title, body, active, extra_css=setup_form.FORM_CSS + editor.EDITOR_CSS, head=head)


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


def _review_note(tailored_path: str | None) -> str:
    """What the quality judge flagged in a tailored resume, if anything (from its _REPORT.json)."""
    if not tailored_path:
        return ""
    report = Path(tailored_path).with_name(Path(tailored_path).stem + "_REPORT.json")
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if data.get("status") != "approved_with_judge_warning":
        return ""
    issues = ((data.get("judge") or {}).get("issues") or "").strip()
    if not issues or issues.lower() == "none":
        return ""
    return issues[:700] + ("…" if len(issues) > 700 else "")


def _rel(path_str: str | None) -> str:
    try:
        return Path(path_str).resolve().relative_to(DATA_DIR.resolve()).as_posix() if path_str else ""
    except ValueError:
        return ""


def _job_for(doc: Path) -> tuple[str, str]:
    """(job title line, 'resume' or 'cover letter') for a tailored document."""
    import sqlite3
    kind = "resume" if doc.parent.name == "tailored_resumes" else "cover letter"
    col = "tailored_resume_path" if kind == "resume" else "cover_letter_path"
    try:
        conn = sqlite3.connect(f"file:{DATA_DIR / 'applypilot.db'}?mode=ro", uri=True, timeout=5)
        row = conn.execute(f"SELECT title, site FROM jobs WHERE {col} = ?", (str(doc),)).fetchone()
        conn.close()
    except sqlite3.Error:
        row = None
    return (f"{row[0]} · {row[1]}" if row else doc.stem.replace("_", " ")), kind


def edit_html(doc: Path, rel: str, message: str = "") -> bytes:
    title, kind = _job_for(doc)
    return page(f"Edit {kind}", editor.page_body(doc, rel, title, kind, message), "ready")


def _dup_key(title: str | None, site: str | None) -> str:
    """Same title at the same source is treated as the same job posted twice."""
    return f"{(title or '').strip().lower()}|{(site or '').strip().lower()}"


def _job_card(j, applied_keys: set[str]) -> str:
    score = j["fit_score"] or 0
    apply_url = j["application_url"] or j["url"] or ""
    meta = " &middot; ".join(html.escape(x) for x in (j["site"], j["location"]) if x)
    applied = bool(j["applied_at"])
    url_q = html.escape(j["url"], quote=True)
    actions = []
    if href := _doc_href(j["tailored_resume_path"]):
        actions.append(f'<a class="btn good sm" href="{html.escape(href)}" target="_blank">Resume</a>')
    if href := _doc_href(j["cover_letter_path"]):
        actions.append(f'<a class="btn ghost sm" href="{html.escape(href)}" target="_blank">Cover letter</a>')
    if apply_url and not applied:
        actions.append(f'<a class="btn primary sm" href="{html.escape(apply_url)}" target="_blank" '
                       f'rel="noopener">Apply &rarr;</a>')
    note = "" if applied else _review_note(j["tailored_resume_path"])
    resume_rel = html.escape(_rel(j["tailored_resume_path"]), quote=True)
    cover_rel = html.escape(_rel(j["cover_letter_path"]), quote=True)
    review = (f'<details class="review"><summary>⚠ Check this resume before sending</summary>'
              f'<p>{html.escape(note).replace(chr(10), "<br>")}</p>'
              f'<a class="btn primary sm" href="/edit?f={resume_rel}">Fix it in the editor</a></details>'
              if note else "")
    dup = ""
    status = j["apply_status"] or ""
    if not applied and status and status not in ("applied", "applied_auto", "in_progress"):
        reason = (j["apply_error"] or status).replace("_", " ")
        dup = (f'<div class="muted small">Auto-apply couldn&rsquo;t finish this one ({html.escape(reason[:120])}). '
               f'Apply by hand.</div>')
    if not applied and not dup and needs_account(apply_url):
        dup = ('<div class="muted small">🔑 Apply by hand: this company\'s site needs an account, '
               'so auto-apply skips it.</div>')
    if not applied and _dup_key(j["title"], j["site"]) in applied_keys:
        dup = ('<div class="review" style="margin:0"><b style="color:var(--warn)">You already applied to a job '
               'with this title here.</b> It\'s probably the same posting listed twice.</div>')
    edits = [f'<a href="/edit?f={resume_rel}">Edit resume</a>'] if resume_rel and not applied else []
    if cover_rel and not applied:
        edits.append(f'<a href="/edit?f={cover_rel}">Edit cover letter</a>')
    if resume_rel:
        edits.append(f'<a href="/docx?f={resume_rel}">Resume as Word</a>')
    if applied and (j["url"] or apply_url):
        edits.append(f'<a href="{html.escape(j["url"] or apply_url)}" target="_blank" rel="noopener">'
                     f'View job posting</a>')
    edit_links = f'<div class="muted small">{" &middot; ".join(edits)}</div>' if edits else ""
    if applied:
        when = (j["applied_at"] or "")[:10]
        how = " by auto-apply" if (j["apply_status"] or "") == "applied_auto" else ""
        mark = (f'<form method="post" action="/mark" class="mark"><input type="hidden" name="url" value="{url_q}">'
                f'<span class="applied-tag">✓ Applied {html.escape(when)}{how}</span>'
                f'<button class="linkbtn" name="action" value="undo">Undo</button></form>')
    else:
        mark = (f'<form method="post" action="/mark" class="mark"><input type="hidden" name="url" value="{url_q}">'
                f'<button class="btn ghost sm check" name="action" value="applied">☐ I applied to this job</button>'
                f'</form>')
    return f"""<div class="card job{' done' if applied else ''}"><div class="job-top">
<div class="score{'' if score >= 7 else ' mid'}">{score}</div><div>
<a class="job-title" href="{html.escape(j['url'] or apply_url)}" target="_blank" rel="noopener">
{html.escape(j['title'] or 'Untitled')}</a><div class="meta">{meta}</div></div></div>
{dup}{review}<div class="row" style="gap:8px">{''.join(actions)}</div>{mark}{edit_links}</div>"""


def _tailored_jobs() -> list:
    import sqlite3
    db = DATA_DIR / "applypilot.db"
    jobs = []
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            jobs = conn.execute(
                "SELECT title, site, location, url, application_url, fit_score, tailored_resume_path, "
                "cover_letter_path, applied_at, apply_status, apply_error FROM jobs WHERE tailored_resume_path IS NOT NULL "
                "ORDER BY fit_score DESC, tailored_at DESC").fetchall()
            conn.close()
        except sqlite3.Error as e:
            log(f"Ready page query failed: {e}")
    return jobs


def applied_html() -> bytes:
    jobs = _tailored_jobs()
    done = sorted((j for j in jobs if j["applied_at"]), key=lambda j: j["applied_at"], reverse=True)
    keys = {_dup_key(j["title"], j["site"]) for j in done}
    if not done:
        body = """<h1>Applied</h1><div class="card empty"><div class="big">📬</div><h2>No applications yet</h2>
<p class="muted">When you tick <b>I applied to this job</b> on Ready to apply, or auto-apply submits one, it shows
up here with the resume and cover letter that went with it.</p><a class="btn ghost" href="/ready">Go to Ready to
apply</a></div>"""
        return page("Applied", body, "applied")
    body = f"""<h1>Applied</h1>
<p class="sub">{len(done)} application{'s' if len(done) != 1 else ''}, newest first, with the resume and cover letter
you sent. Tap <b>Undo</b> to move one back to Ready to apply.</p>
<div class="stack">{"".join(_job_card(j, keys) for j in done)}</div>"""
    return page("Applied", body, "applied")


def ready_html() -> bytes:
    jobs = _tailored_jobs()
    if not jobs:
        c = job_counts() or {"good_fit": 0}
        note = (f"{c['good_fit']} good fits found so far. Their resumes are being written now."
                if c["good_fit"] else f"Jobs scoring {setup_form.saved_min_score(DATA_DIR)} or more will show up here "
                "with their own resume and cover letter.")
        body = f"""<h1>Ready to apply</h1>
<div class="card empty"><div class="big">📝</div><h2>Nothing ready yet</h2>
<p class="muted">{note}</p><a class="btn ghost" href="/status">Check progress</a></div>"""
        return page("Ready to apply", body, "ready")

    todo = [j for j in jobs if not j["applied_at"]]
    done = sorted((j for j in jobs if j["applied_at"]), key=lambda j: j["applied_at"], reverse=True)
    applied_keys = {_dup_key(j["title"], j["site"]) for j in done}
    todo_html = "".join(_job_card(j, applied_keys) for j in todo) or (
        '<div class="card empty"><div class="big">🎉</div><h2>All caught up</h2>'
        '<p class="muted">New matches appear here after each search.</p></div>')
    done_html = (f'<a class="btn ghost" style="margin-top:18px;display:flex" href="/applied">'
                 f'See your {len(done)} applied job{"s" if len(done) != 1 else ""} &rarr;</a>' if done else "")
    body = f"""<h1>Ready to apply</h1>
<p class="sub">{len(todo)} to apply, best match first{f", {len(done)} applied" if done else ""}. Download the resume,
apply, then tick <b>I applied</b> so you never apply twice.</p>
<div class="stack">{todo_html}</div>{done_html}"""
    return page("Ready to apply", body, "ready")


APP_ROOT = Path(__file__).resolve().parent.parent  # the ApplyPilot source the server runs
_package_cache: bytes | None = None


def package_tarball() -> bytes:
    """This server's ApplyPilot source as a pip-installable sdist-style tar.gz (for the companion)."""
    global _package_cache
    if _package_cache is None:
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                tar.add(APP_ROOT / name, arcname=f"applypilot/{name}")
            tar.add(APP_ROOT / "src", arcname="applypilot/src",
                    filter=lambda t: None if "__pycache__" in t.name else t)
        _package_cache = buf.getvalue()
    return _package_cache


# Application sites that require creating an account (usually with an emailed code) before applying.
# Auto-apply can't get past those, so they're left for the person to do by hand.
ACCOUNT_SITES = ("myworkdayjobs.com", "workday.com", "oraclecloud.com", "taleo.net", "successfactors",
                 "paycomonline.net", "icims.com", "ultipro.com", "brassring.com")


def needs_account(url: str | None) -> bool:
    return bool(url) and any(site in url.lower() for site in ACCOUNT_SITES)


def apply_bundle() -> bytes:
    """Everything the companion needs to auto-apply: profile, jobs left to apply to, and their documents."""
    import io
    import sqlite3
    import zipfile
    conn = sqlite3.connect(f"file:{DATA_DIR / 'applypilot.db'}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT url, title, site, location, application_url, fit_score, full_description, "
        "tailored_resume_path, cover_letter_path, COALESCE(apply_attempts, 0) AS apply_attempts "
        "FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL "
        "AND COALESCE(apply_status, '') NOT IN ('manual', 'expired') "
        "ORDER BY fit_score DESC").fetchall()
    # Never queue the same posting twice: skip duplicates of anything applied to, and of each other.
    seen = {_dup_key(t, st) for t, st in conn.execute(
        "SELECT title, site FROM jobs WHERE applied_at IS NOT NULL").fetchall()}
    unique = []
    for r in rows:
        key = _dup_key(r["title"], r["site"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    rows = [r for r in unique if not needs_account(r["application_url"] or r["url"])]
    account_only = len(unique) - len(rows)
    conn.close()
    buf = io.BytesIO()
    jobs = []
    written: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(DATA_DIR / "profile.json", "profile.json")
        if (DATA_DIR / "resume.txt").exists():
            z.write(DATA_DIR / "resume.txt", "resume.txt")
        for r in rows:
            job = {k: r[k] for k in ("url", "title", "site", "location", "application_url", "fit_score",
                                     "full_description", "apply_attempts")}
            for key, col in (("tailored_resume", "tailored_resume_path"), ("cover_letter", "cover_letter_path")):
                rel = _rel(r[col])
                job[key] = rel or None
                if rel:
                    for suffix in (".txt", ".pdf"):
                        f = (DATA_DIR / rel).with_suffix(suffix)
                        name = f.relative_to(DATA_DIR).as_posix()
                        if f.exists() and name not in written:
                            z.write(f, name)
                            written.add(name)
            jobs.append(job)
        z.writestr("manifest.json", json.dumps({
            "jobs": jobs, "needs_account": account_only,
            "min_score": int(setup_form.saved_min_score(DATA_DIR)), "created": now_iso()}))
    return buf.getvalue()


def record_results(results: list[dict]) -> int:
    """Apply the companion's outcomes: real submissions become applied; failures are noted."""
    import sqlite3
    updated = 0
    for r in results:
        url, status = r.get("url"), (r.get("status") or "").strip()
        if not url or not status:
            continue
        if status == "applied":
            updated += mark_applied(url, True, "applied_auto", r.get("at"))
            continue
        try:
            conn = sqlite3.connect(DATA_DIR / "applypilot.db", timeout=10)
            updated += conn.execute(
                "UPDATE jobs SET apply_status = ?, apply_error = ?, "
                "apply_attempts = COALESCE(apply_attempts, 0) + 1 WHERE url = ? AND applied_at IS NULL",
                (status, (r.get("error") or "")[:300], url)).rowcount
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            log(f"Could not record result for {url}: {e}")
    return updated


def mark_applied(url: str, applied: bool, status: str = "applied", when: str | None = None) -> bool:
    """Record (or undo) an application for a job. Returns whether a job was updated."""
    import sqlite3
    try:
        conn = sqlite3.connect(DATA_DIR / "applypilot.db", timeout=10)
        if applied:
            n = conn.execute("UPDATE jobs SET applied_at = ?, apply_status = ?, apply_error = NULL WHERE url = ?",
                             (when or now_iso(), status, url)).rowcount
        else:
            n = conn.execute("UPDATE jobs SET applied_at = NULL, apply_status = NULL WHERE url = ?",
                             (url,)).rowcount
        conn.commit()
        conn.close()
        return n > 0
    except sqlite3.Error as e:
        log(f"Could not update applied state: {e}")
        return False


def autoapply_html(host: str) -> bytes:
    c = job_counts() or {}
    body = f"""<h1>Auto-apply</h1>
<p class="sub">Let Claude fill in and submit applications for you, from your own Windows PC.</p>
<div class="card stack">
<p style="margin:0">The companion downloads your <b>Ready to apply</b> jobs and their resumes, then uses Claude Code
and Chrome on your PC to fill in each application. Jobs it submits are marked <b>Applied</b> here automatically,
so nothing is sent twice.</p>
<a class="btn primary" href="/companion.py" download="ApplyPilot-Companion.py">Download the companion</a>
</div>
<details class="card" open><summary><b>One-time setup (Windows)</b></summary>
<ol class="steps" style="margin-top:16px">
<li><b>Download the companion</b> <span class="muted">with the button above. It saves as
<code>ApplyPilot-Companion.py</code> in your Downloads folder.</span></li>
<li><b>Open PowerShell</b> <span class="muted">(Start menu, type PowerShell) and run:<br>
<code>cd ~\\Downloads</code><br><code>python ApplyPilot-Companion.py</code></span></li>
<li><b>Enter your site password</b> <span class="muted">when asked. It's remembered after the first time. The
first run also installs ApplyPilot, which takes a few minutes.</span></li>
<li><b>Start with a practice run.</b> <span class="muted">Chrome opens and Claude fills in your best match but
<b>doesn't submit</b>. Watch it and check the answers.</span></li>
<li><b>Then let it apply.</b> <span class="muted">Choose how many jobs. Each takes a few minutes; keep the PC awake
and don't use that Chrome window while it works.</span></li>
</ol></details>
<details class="card"><summary><b>Good to know</b></summary>
<ul class="muted small" style="margin:12px 0 0;padding-left:20px">
<li>It submits under your name using your Setup details (salary, start date, work authorization), so keep them
current.</li>
<li>Sites that need an account login or a CAPTCHA often can't be finished; those are noted on the job card for
you to apply by hand.</li>
<li>It uses your Claude subscription through Claude Code; each application is several minutes of Claude's work.</li>
<li>Run the companion again any time to pick up new matches. {c.get('tailored', 0)} jobs have resumes ready now.</li>
</ul></details>"""
    return page("Auto-apply", body, "auto")


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

        if path == "/autoapply":
            return self._send(200, autoapply_html(self.headers.get("Host", "")))
        if path == "/companion.py":
            host = self.headers.get("Host", "")
            script = (Path(__file__).parent / "companion.py").read_text(encoding="utf-8")
            script = script.replace("__SERVER_URL__", f"https://{host}")
            return self._send(200, script.encode(), "text/x-python; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="ApplyPilot-Companion.py"'})
        if path == "/api/logs":
            names = sorted((DATA_DIR / "companion_logs").glob("*.txt"), reverse=True)
            return self._send(200, "\n".join(n.name for n in names).encode(), "text/plain; charset=utf-8")
        if path.startswith("/api/logs/"):
            f = DATA_DIR / "companion_logs" / Path(path).name
            if f.is_file():
                return self._send(200, f.read_bytes(), "text/plain; charset=utf-8")
            return self._send(404, b"Not found", "text/plain")
        if path == "/api/package.tar.gz":
            return self._send(200, package_tarball(), "application/gzip")
        if path == "/api/bundle.zip":
            return self._send(200, apply_bundle(), "application/zip")
        if path in ("/edit", "/docx"):
            rel = parse_qs(urlparse(self.path).query).get("f", [""])[0]
            doc = editor.resolve(DATA_DIR, rel)
            if not doc:
                return self._send(404, b"Not found", "text/plain")
            if path == "/edit":
                return self._send(200, edit_html(doc, rel))
            return self._send(200, editor.to_docx(doc.read_text(encoding="utf-8")),
                              "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                              {"Content-Disposition": f'attachment; filename="{doc.stem}.docx"'})
        if path == "/resume.txt":
            resume = DATA_DIR / "resume.txt"
            if resume.exists():
                return self._send(200, resume.read_bytes(), "text/plain; charset=utf-8")
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
        if path == "/applied":
            return self._send(200, applied_html())
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
        if path not in ("/run", "/setup", "/edit", "/mark", "/api/results", "/api/log"):
            return self._send(404, b"Not found", "text/plain")
        if not self._authorized():
            return
        if path == "/setup":
            return self._post_setup()
        if path == "/edit":
            return self._post_edit()
        if path == "/api/log":
            length = int(self.headers.get("Content-Length") or 0)
            text = self.rfile.read(min(length, 2 * 1024 * 1024)).decode("utf-8", errors="replace")
            logs = DATA_DIR / "companion_logs"
            logs.mkdir(exist_ok=True)
            name = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + ".txt"
            (logs / name).write_text(text, encoding="utf-8")
            for old in sorted(logs.glob("*.txt"))[:-30]:  # keep the latest 30
                old.unlink(missing_ok=True)
            return self._send(200, b'{"ok": true}', "application/json")
        if path == "/api/results":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                results = json.loads(self.rfile.read(min(length, 2 * 1024 * 1024))).get("results", [])
            except ValueError:
                return self._send(400, b'{"error": "bad json"}', "application/json")
            n = record_results(results if isinstance(results, list) else [])
            log(f"Companion reported {len(results)} result(s), {n} recorded")
            return self._send(200, json.dumps({"updated": n}).encode(), "application/json")
        if path == "/mark":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(min(length, 65536)).decode("utf-8", errors="replace"))
            url = form.get("url", [""])[0]
            mark_applied(url, form.get("action", ["applied"])[0] == "applied")
            back = "/applied" if "/applied" in (self.headers.get("Referer") or "") else "/ready"
            return self._send(303, b"", "text/plain", {"Location": back})
        if _run_lock.locked():
            msg = "Pipeline is already running."
        else:
            threading.Thread(target=run_pipeline, daemon=True).start()
            msg = "Pipeline started."
        self._send(303, msg.encode(), "text/plain", {"Location": "/status"})

    def _post_edit(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2 * 1024 * 1024:
            return self._send(413, b"Too large", "text/plain")
        form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"), keep_blank_values=True)
        rel = form.get("f", [""])[0]
        doc = editor.resolve(DATA_DIR, rel)
        if not doc:
            return self._send(404, b"Not found", "text/plain")
        if form.get("action", ["save"])[0] == "checked":
            editor.mark_checked(doc)
            return self._send(303, b"", "text/plain", {"Location": "/ready"})
        content = form.get("content", [""])[0]
        if not content.strip():
            return self._send(200, edit_html(doc, rel, "Nothing saved: the text was empty."))
        problem = editor.save(doc, content)
        editor.mark_checked(doc)
        log(f"Edited {rel}")
        return self._send(200, edit_html(doc, rel, problem or "Saved. The PDF has been rebuilt with your changes."))

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
