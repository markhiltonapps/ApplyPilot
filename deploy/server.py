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
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import agent_control
import editor
import onboarding
import setup_form
import ui

from applypilot.company import board_label, company_for

DATA_DIR = Path(os.environ.setdefault("APPLYPILOT_DIR", "/data"))
control = agent_control.Control(DATA_DIR / "autoapply_control.json")
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

        from applypilot.scoring.scorer import SCORE_PROMPT_VERSION, ai_only
        resume = DATA_DIR / "resume.txt"
        resume_hash = hashlib.sha256(resume.read_bytes()).hexdigest()[:16] if resume.exists() else ""
        base = f"{SCORE_PROMPT_VERSION}:{resume_hash}"
        version = base + (":ai" if ai_only() else "")
        marker = DATA_DIR / ".score_prompt_version"
        old_version = marker.read_text().strip() if marker.exists() else ""
        # Old markers held only the prompt version; treat a matching one as current for this resume.
        if old_version == str(SCORE_PROMPT_VERSION):
            marker.write_text(version)
        elif old_version != version:
            # Re-score everything not yet applied to, including jobs with resumes ready: a job that
            # now scores below the bar drops off Ready to apply (its resume is kept on disk).
            m = conn.execute("UPDATE jobs SET fit_score = NULL, scored_at = NULL "
                             "WHERE fit_score IS NOT NULL AND applied_at IS NULL").rowcount
            marker.write_text(version)
            log(f"Resume or scoring preferences changed; re-scoring {m} job(s)")
        # One-time: re-score the jobs on Ready to apply under the rule about where the candidate lives,
        # so remote jobs limited to other states drop off the list without hiding each by hand.
        location_marker = DATA_DIR / ".rescore_location_v1"
        if not location_marker.exists():
            r = conn.execute("UPDATE jobs SET fit_score = NULL, scored_at = NULL WHERE applied_at IS NULL "
                             "AND fit_score >= ? AND COALESCE(apply_status, '') != 'skipped'",
                             (int(setup_form.saved_min_score(DATA_DIR)),)).rowcount
            location_marker.write_text("1")
            log(f"Re-checking {r} good-fit job(s) for location limits")
        # One-time: rewrite the resumes not yet used under the tighter length rules (two pages, fewer and
        # shorter bullets). Cover letters are kept; resumes edited by hand are kept.
        length_marker = DATA_DIR / ".resume_length_v1"
        if not length_marker.exists():
            r = 0
            for url, path, when in conn.execute(
                    "SELECT url, tailored_resume_path, tailored_at FROM jobs "
                    "WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL").fetchall():
                if _edited_by_hand(path, when):
                    continue
                conn.execute("UPDATE jobs SET tailored_resume_path = NULL, tailored_at = NULL, tailor_attempts = 0 "
                             "WHERE url = ?", (url,))
                r += 1
            length_marker.write_text("1")
            log(f"Rewriting {r} resume(s) to fit two pages")
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
        # Whenever the master resume changes (and once for older setups), rewrite the resumes and cover
        # letters not yet used so they draw on it. Ones the person edited by hand are kept.
        refresh_marker = DATA_DIR / ".refresh_v3"
        resume_marker = DATA_DIR / ".tailored_from_resume"
        resume_changed = resume_marker.exists() and resume_marker.read_text().strip() != resume_hash
        if not resume_marker.exists() or resume_changed:
            resume_marker.write_text(resume_hash)
        if not refresh_marker.exists() or resume_changed:
            r = 0
            for url, path, when in conn.execute(
                    "SELECT url, tailored_resume_path, tailored_at FROM jobs "
                    "WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL").fetchall():
                if _edited_by_hand(path, when):
                    continue
                conn.execute("UPDATE jobs SET tailored_resume_path = NULL, tailored_at = NULL, tailor_attempts = 0, "
                             "cover_letter_path = NULL, cover_letter_at = NULL, cover_attempts = 0 WHERE url = ?",
                             (url,))
                r += 1
            refresh_marker.write_text("1")
            log(f"Rewriting {r} resume(s) and cover letter(s) from the updated master resume")
        conn.commit()
        conn.close()
        if n:
            log(f"Re-queued {n} job(s) whose scoring failed")
    except sqlite3.Error as e:
        log(f"Could not re-queue failed scores: {e}")


def _edited_by_hand(path: str | None, tailored_at: str | None) -> bool:
    """Whether a tailored resume was changed in the editor after it was written."""
    if not path:
        return False
    txt = Path(path).with_suffix(".txt")
    if editor.was_edited(txt):
        return True
    try:  # edits from before the editor recorded them: the file changed well after it was written
        written = datetime.fromisoformat(tailored_at).timestamp() if tailored_at else None
        return bool(written) and txt.exists() and txt.stat().st_mtime - written > 120
    except (ValueError, OSError):
        return False


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


def setup_banner(onboarded: bool) -> str:
    if onboarded:
        return ('<div class="card" style="border-color:#22c55e55;margin-bottom:16px"><b>Your master resume and '
                'details are filled in below.</b> <span class="muted">Check the AI service, salary range and '
                'voluntary questions, then click <b>Save and start searching</b> at the bottom.</span></div>')
    has_resume = (DATA_DIR / "resume.txt").exists()
    lead = "Want a stronger resume?" if has_resume else "New here? Start with this."
    return (f'<div class="card" style="display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:16px">'
            f'<div style="flex:1;min-width:220px"><b>{lead}</b> <span class="muted">Upload your resume and LinkedIn '
            'PDF, answer a few questions, and ApplyPilot builds your master resume and fills in this page.</span>'
            '</div><a class="btn primary sm" href="/onboard">Build my master resume</a></div>')


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


def ai_banner() -> str:
    """A warning when the AI service is refusing requests: nothing gets scored or written until it's fixed."""
    problem = setup_form.ai_problem(DATA_DIR)
    if not problem:
        return ""
    return (f'<div class="card" style="border-color:#ef444466;background:#ef44441a;margin-bottom:16px">'
            f'<b style="color:#fca5a5">The AI service isn&rsquo;t working.</b> {html.escape(problem)} Until it&rsquo;s fixed, '
            'no jobs are scored and no resumes are written. Fix it at <a href="https://openrouter.ai/settings/credits" '
            'target="_blank" rel="noopener">openrouter.ai</a> (Credits, or Keys for the spending limit), then open '
            '<a href="/setup">Setup</a> and click <b>Save</b> to restart.</div>')


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
{ai_banner()}
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


def _back(value: str | None) -> str:
    """Where the editor returns to: only pages of this site."""
    return value if value in ("/ready", "/autoapply") else "/ready"


def edit_html(doc: Path, rel: str, message: str = "", back: str = "/ready") -> bytes:
    title, kind = _job_for(doc)
    return page(f"Edit {kind}", editor.page_body(doc, rel, title, kind, message, back),
                "auto" if back == "/autoapply" else "ready")


_COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|plc|lp|llp|group|holdings)\b")


def _dup_key(j) -> str:
    """The same title at the same company is the same job, whichever board listed it.

    When the company isn't known, the board stands in for it.
    """
    title = re.sub(r"[^a-z0-9]+", " ", (j["title"] or "").lower()).strip()
    who = company_for(j["company"], j["site"], j["application_url"]) or j["site"] or ""
    who = _COMPANY_SUFFIXES.sub(" ", re.sub(r"[^a-z0-9]+", " ", who.lower()))
    return f"{title}|{' '.join(who.split())}"


def job_meta(j) -> str:
    """'Apollo Io · Remote · via Indeed': who's hiring first, then where, then where the lead came from."""
    who = company_for(j["company"], j["site"], j["application_url"])
    board = board_label(j["site"])
    parts = [f"<b>{html.escape(who)}</b>" if who else "", html.escape(j["location"] or ""),
             f"via {html.escape(board)}" if board else ""]
    return " &middot; ".join(p for p in parts if p)


def _job_card(j, applied_keys: set[str]) -> str:
    score = j["fit_score"] or 0
    apply_url = j["application_url"] or j["url"] or ""
    meta = job_meta(j)
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
    if not applied and status and status not in ("applied", "applied_auto", "in_progress", "skipped"):
        raw = j["apply_error"] or status
        reason = FAILURE_WORDS.get(raw, raw.replace("_", " "))
        dup = (f'<div class="muted small">Auto-apply couldn&rsquo;t finish this one: {html.escape(reason[:120])}. '
               f'Apply by hand.</div>')
    if not applied and not dup and needs_account(apply_url):
        dup = ('<div class="muted small">🔑 Apply by hand: this company\'s site needs an account, '
               'so auto-apply skips it.</div>')
    if not applied and _dup_key(j) in applied_keys:
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
    elif status == "skipped":
        mark = (f'<form method="post" action="/mark" class="mark"><input type="hidden" name="url" value="{url_q}">'
                f'<span class="muted small">Hidden: auto-apply skips it.</span> '
                f'<button class="linkbtn" name="action" value="unskip">Bring it back</button></form>')
    else:
        mark = (f'<form method="post" action="/mark" class="mark"><input type="hidden" name="url" value="{url_q}">'
                f'<button class="btn ghost sm check" name="action" value="applied">☐ I applied to this job</button> '
                f'<button class="linkbtn" name="action" value="skip" title="Hide it and never auto-apply">'
                f'✕ Not for me</button></form>')
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
                "SELECT title, site, company, location, url, application_url, fit_score, tailored_resume_path, "
                "cover_letter_path, applied_at, apply_status, apply_error, COALESCE(apply_attempts, 0) AS apply_attempts "
                "FROM jobs WHERE tailored_resume_path IS NOT NULL "
                "AND (applied_at IS NOT NULL OR fit_score IS NULL OR fit_score >= ?) "
                "ORDER BY fit_score DESC, tailored_at DESC", (int(setup_form.saved_min_score(DATA_DIR)),)).fetchall()
            conn.close()
        except sqlite3.Error as e:
            log(f"Ready page query failed: {e}")
    return jobs


def applied_html() -> bytes:
    jobs = _tailored_jobs()
    done = sorted((j for j in jobs if j["applied_at"]), key=lambda j: j["applied_at"], reverse=True)
    keys = {_dup_key(j) for j in done}
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


# Reasons a job isn't worth doing at all (closed, wrong kind of role), as opposed to ones that need a person.
DEAD_END_WORDS = ("expired", "closed", "no_longer", "contract", "1099", "freelance", "part_time", "part-time",
                  "not_eligible", "eligib", "authorization", "clearance", "relocat", "already_applied", "duplicate",
                  "not_a_job", "no_job_url", "no job url")


def needs_you(j) -> bool:
    """Auto-apply has done what it can: this job is waiting on the person (an account, a CAPTCHA, a stuck form)."""
    if j["applied_at"]:
        return False
    if needs_account(j["application_url"] or j["url"]):
        return True
    status = j["apply_status"] or ""
    if not status or status in ("applied", "applied_auto", "in_progress"):
        return False
    reason = (j["apply_error"] or status).lower()
    if any(w in reason for w in DEAD_END_WORDS):
        return False
    return status == "manual" or reason in HAND_ONLY_REASONS or not worth_retrying(reason, j["apply_attempts"])


def ready_html(view: str = "") -> bytes:
    jobs = _tailored_jobs()
    if not jobs:
        c = job_counts() or {"good_fit": 0}
        note = (f"{c['good_fit']} good fits found so far. Their resumes are being written now."
                if c["good_fit"] else f"Jobs scoring {setup_form.saved_min_score(DATA_DIR)} or more will show up here "
                "with their own resume and cover letter.")
        body = f"""<h1>Ready to apply</h1>{ai_banner()}
<div class="card empty"><div class="big">📝</div><h2>Nothing ready yet</h2>
<p class="muted">{note}</p><a class="btn ghost" href="/status">Check progress</a></div>"""
        return page("Ready to apply", body, "ready")

    done = sorted((j for j in jobs if j["applied_at"]), key=lambda j: j["applied_at"], reverse=True)
    applied_keys = {_dup_key(j) for j in done}
    # The same posting found on two boards is listed once (jobs come best match first, so the best copy stays).
    todo, seen = [], set()
    hidden = [j for j in jobs if not j["applied_at"] and (j["apply_status"] or "") == "skipped"]
    for j in jobs:
        if not j["applied_at"] and (j["apply_status"] or "") != "skipped" and _dup_key(j) not in seen:
            seen.add(_dup_key(j))
            todo.append(j)
    mine = [j for j in todo if needs_you(j) and _dup_key(j) not in applied_keys]
    tabs = ai_banner()
    tabs += (f'<div class="tabs"><a href="/ready"{" class=on" if view not in ("needs", "hidden") else ""}>All to apply '
            f'<span>{len(todo)}</span></a><a href="/ready?view=needs"{" class=on" if view == "needs" else ""}>'
            f'Needs you <span>{len(mine)}</span></a></div>')
    if view == "hidden":
        cards = "".join(_job_card(j, applied_keys) for j in hidden) or (
            '<div class="card empty"><h2>No hidden jobs</h2></div>')
        body = f"""<h1>Ready to apply</h1>{tabs}
<p class="sub">{len(hidden)} job{"s" if len(hidden) != 1 else ""} you marked <b>Not for me</b>. Auto-apply skips
these. Click <b>Bring it back</b> to put one back on the list.</p><div class="stack">{cards}</div>"""
        return page("Hidden jobs", body, "ready", READY_CSS)
    if view == "needs":
        cards = "".join(_job_card(j, applied_keys) for j in mine) or (
            '<div class="card empty"><div class="big">✅</div><h2>Nothing waiting on you</h2>'
            '<p class="muted">Jobs auto-apply can&rsquo;t finish (an account, a CAPTCHA) show up here.</p></div>')
        body = f"""<h1>Ready to apply</h1>{tabs}
<p class="sub">{len(mine)} job{"s" if len(mine) != 1 else ""} auto-apply couldn&rsquo;t finish, best match first. The
resume and cover letter are already written: click <b>Apply</b>, fill in the form (use <b>Resume</b> and
<b>Cover letter</b> to download them), then tick <b>I applied</b>.</p>
<div class="stack">{cards}</div>"""
        return page("Needs you", body, "ready", READY_CSS)
    todo_html = "".join(_job_card(j, applied_keys) for j in todo) or (
        '<div class="card empty"><div class="big">🎉</div><h2>All caught up</h2>'
        '<p class="muted">New matches appear here after each search.</p></div>')
    done_html = (f'<a class="btn ghost" style="margin-top:18px;display:flex" href="/applied">'
                 f'See your {len(done)} applied job{"s" if len(done) != 1 else ""} &rarr;</a>' if done else "")
    if hidden:
        done_html += (f'<p class="muted small" style="margin-top:12px"><a href="/ready?view=hidden">{len(hidden)} '
                      f'job{"s" if len(hidden) != 1 else ""} hidden as Not for me</a></p>')
    body = f"""<h1>Ready to apply</h1>
<p class="sub">{len(todo)} to apply, best match first{f", {len(done)} applied" if done else ""}. These are all
jobs scoring {setup_form.saved_min_score(DATA_DIR)} or more ({sum(1 for j in todo if (j["fit_score"] or 0) >= 7)} of
them score 7+, which the Jobs tab calls a strong fit). Download the resume,
apply, then tick <b>I applied</b> so you never apply twice.</p>
<div class="stack">{todo_html}</div>{done_html}"""
    return page("Ready to apply", body.replace("<h1>Ready to apply</h1>", "<h1>Ready to apply</h1>" + tabs, 1),
                "ready", READY_CSS)


READY_CSS = """<style>
.tabs{display:flex;gap:6px;margin:4px 0 16px;border-bottom:1px solid var(--line)}
.tabs a{padding:9px 14px;color:var(--muted);text-decoration:none;font-weight:600;border-bottom:2px solid transparent;
  margin-bottom:-1px}
.tabs a.on{color:var(--text);border-bottom-color:var(--accent)}
.tabs a span{display:inline-block;min-width:22px;padding:1px 7px;margin-left:5px;border-radius:999px;font-size:12.5px;
  background:var(--surface-2);color:var(--muted);text-align:center}
.tabs a.on span{background:var(--accent);color:var(--accent-ink)}
</style>"""

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


# Auto-apply outcomes that won't change on a retry, so the job is left for the person to do by hand.
HAND_ONLY_REASONS = ("login_issue", "account_required", "sso_required", "expired", "already_applied",
                     "not_a_job_application", "not_eligible_location", "not_eligible_salary",
                     "not_eligible_work_auth", "captcha", "email_verification")
FAILURE_WORDS = {
    "login_issue": "the site needs an account",
    "account_required": "the site needs an account",
    "sso_required": "the site needs a Google or Microsoft sign-in",
    "expired": "the posting has closed",
    "captcha": "the site showed a CAPTCHA",
    "already_applied": "the site says you already applied",
    "not_eligible_location": "the role isn't open to your location",
    "not_eligible_work_auth": "the role's work authorization doesn't fit",
    "not_eligible_salary": "the salary range doesn't fit",
    "email_verification": "the site emails you a code to finish",
}


# Auto-apply reasons that won't change on another try (the agent words them freely, so match on keywords).
PERMANENT_FAILURE_WORDS = ("contract", "1099", "freelance", "part_time", "part-time", "closed", "expired",
                           "no_longer", "not_eligible", "eligib", "authorization", "clearance", "citizenship",
                           "relocat", "account", "login", "sign_in", "sso", "already_applied", "duplicate",
                           "not_a_job", "no_job_url", "no job url")


def worth_retrying(error: str | None, attempts: int | None) -> bool:
    """Whether auto-apply should try a job again: not after a permanent reason, and at most twice in all."""
    if not error:
        return True
    if (attempts or 0) >= 2:
        return False
    low = error.lower()
    return not any(w in low for w in PERMANENT_FAILURE_WORDS)


def needs_account(url: str | None) -> bool:
    return bool(url) and any(site in url.lower() for site in ACCOUNT_SITES)


def apply_candidates() -> tuple[list, int]:
    """Jobs auto-apply can take on, best first, and how many more were left out because they need an account."""
    import sqlite3
    if not (DATA_DIR / "applypilot.db").exists():
        return [], 0
    conn = sqlite3.connect(f"file:{DATA_DIR / 'applypilot.db'}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT url, title, site, company, location, application_url, fit_score, full_description, "
        "tailored_resume_path, cover_letter_path, apply_error, COALESCE(apply_attempts, 0) AS apply_attempts "
        "FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL "
        f"AND (fit_score IS NULL OR fit_score >= {int(setup_form.saved_min_score(DATA_DIR))}) "
        "AND COALESCE(apply_status, '') NOT IN ('manual', 'expired', 'skipped') "
        f"AND COALESCE(apply_error, '') NOT IN ({', '.join(repr(r) for r in HAND_ONLY_REASONS)}) "
        "ORDER BY fit_score DESC").fetchall()
    # Never queue the same posting twice: skip duplicates of anything applied to, and of each other.
    seen = {_dup_key(r) for r in conn.execute(
        "SELECT title, site, company, application_url FROM jobs WHERE applied_at IS NOT NULL "
        f"OR COALESCE(apply_error, '') IN ({', '.join(repr(r) for r in HAND_ONLY_REASONS)})").fetchall()}
    unique = []
    for r in rows:
        key = _dup_key(r)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    rows = [r for r in unique if not needs_account(r["application_url"] or r["url"])
            and worth_retrying(r["apply_error"], r["apply_attempts"])]
    conn.close()
    return rows, len(unique) - len(rows)


def apply_bundle() -> bytes:
    """Everything the companion needs to auto-apply: profile, jobs left to apply to, and their documents."""
    import io
    import zipfile
    rows, account_only = apply_candidates()
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
            job["company"] = company_for(r["company"], r["site"], r["application_url"])
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


def set_skipped(url: str, skipped: bool) -> None:
    """Hide a job from Ready to apply and from auto-apply ("Not for me"), or bring it back."""
    import sqlite3
    try:
        conn = sqlite3.connect(DATA_DIR / "applypilot.db", timeout=10)
        if skipped:
            conn.execute("UPDATE jobs SET apply_status = 'skipped' WHERE url = ? AND applied_at IS NULL", (url,))
        else:
            conn.execute("UPDATE jobs SET apply_status = NULL WHERE url = ? AND apply_status = 'skipped'", (url,))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        log(f"Could not update skipped state: {e}")


def _since(ts: float | None) -> str:
    if not ts:
        return "never"
    secs = int(time.time() - ts)
    if secs < 90:
        return "just now" if secs < 10 else f"{secs} seconds ago"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size * (2 if size == 86400 else 1.5 if size == 3600 else 1):
            n = max(1, round(secs / size))
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return f"{secs} seconds ago"


def run_summary(mode: str, results: list[dict], stopped: bool) -> tuple[str, bool]:
    """Plain-English outcome of a companion run, and whether it went well."""
    def why(r: dict) -> str:
        raw = r.get("error") or r.get("status") or ""
        return FAILURE_WORDS.get(raw, raw.replace("_", " ").replace("failed:", "") or "it didn't finish")
    done = [r for r in results if r.get("status") == "applied"]
    other = [r for r in results if r.get("status") != "applied"]
    if mode == "practice":
        if done:
            return ("Practice finished: the form was filled in and NOT submitted. Look it over in the Chrome "
                    "window on your PC."), True
        if other:
            return f"Practice stopped: {why(other[0])}. You can apply to that job by hand.", False
        return ("Stopped." if stopped else "Practice ended without finishing the form."), False
    parts = []
    if done:
        parts.append(f"Applied to {len(done)} job{'s' if len(done) != 1 else ''}; "
                     f"{'they are' if len(done) != 1 else 'it is'} on the Applied tab.")
    if other:
        from collections import Counter
        counts = Counter(why(r).strip() for r in other).most_common()
        reasons = "; ".join(f"{reason} ({n})" if n > 1 else reason for reason, n in counts)
        parts.append(f"{len(other)} couldn't be finished: {reasons}. They stay on Ready to apply to do by hand.")
    if stopped:
        parts.append("You stopped the run.")
    return (" ".join(parts) or "Nothing was submitted."), bool(done)


AUTOAPPLY_CSS = """<style>
.aa-jobs{display:flex;flex-direction:column}
.aa-job{display:flex;gap:12px;align-items:center;padding:11px 0;border-top:1px solid var(--line)}
.aa-job:first-child{border-top:0}
.aa-job .score{width:36px;height:36px;font-size:16px;border-radius:9px}
.aa-job .t{flex:1;min-width:0}
.aa-job .t a{color:var(--text);text-decoration:none;font-weight:600}
.aa-job .t a:hover{text-decoration:underline}
.aa-job form{margin:0}
.aa-btns{display:flex;gap:8px;flex:none}
@media (max-width:560px){.aa-job{flex-wrap:wrap}.aa-btns{width:100%}.aa-btns form{flex:1}.aa-btns .btn{width:100%}}
.aa-real{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.aa-real input[type=number]{width:80px;padding:9px 10px;border-radius:10px;border:1px solid var(--line);
  background:var(--field);color:var(--text);font:inherit}
.aa-check{display:flex;gap:9px;align-items:flex-start;margin:12px 0;font-size:14.5px}
.aa-check input{margin-top:4px}
.btn.stop{background:#ef44441a;color:#f87171;border-color:#ef444455}
.btn[disabled]{opacity:.45;cursor:not-allowed}
</style>"""


def autoapply_html(host: str) -> bytes:
    st = control.snapshot()
    online, pending, running, last = st["online"], st.get("pending"), st.get("running"), st.get("last")
    agent = st.get("agent") or {}
    busy = bool(pending or running)
    jobs, account_only = apply_candidates()
    esc = html.escape

    if running:
        mode = "Practice run" if running["mode"] == "practice" else "Applying for real"
        stopping = running.get("stop")
        status = f"""<div class="card status"><span class="dot run"></span><div style="flex:1">
<div class="head">{mode}{": " + esc(running.get("title") or "") if running.get("title") else ""}</div>
<p>{esc(running.get("message") or "Working...")}</p>
<p class="muted small">Started {_since(running.get("started"))}. Chrome is open on your PC; don't use that window
while it works.</p>
{"" if stopping else '<form method="post" action="/autoapply/stop" style="margin-top:12px"><button class="btn stop sm">Stop</button></form>'}
</div></div>"""
    elif pending:
        status = f"""<div class="card status"><span class="dot run"></span><div style="flex:1">
<div class="head">Waiting for your PC to pick this up...</div>
<p class="muted">{"It usually starts within a few seconds." if online else
 "Your PC isn't connected right now, so this will start as soon as it is."}</p>
<form method="post" action="/autoapply/stop" style="margin-top:12px"><button class="btn ghost sm">Cancel</button></form>
</div></div>"""
    elif online:
        status = """<div class="card status"><span class="dot ok"></span><div>
<div class="head">Your PC is connected and ready</div>
<p class="muted">Pick a job below for a practice run, or let it apply for real.</p></div></div>"""
    elif agent.get("last_seen"):
        status = f"""<div class="card status"><span class="dot warn"></span><div>
<div class="head">Your PC isn't connected</div>
<p class="muted">Last seen {_since(agent.get("last_seen"))}. The companion starts by itself when Windows starts.
If the PC is on, open the Start menu, type <b>ApplyPilot Companion</b> and press Enter. If you changed your
site password, run the companion once from PowerShell instead (steps below) so it can sign in again.</p>
<a class="btn ghost sm" style="margin-top:10px" href="/companion.py" download="ApplyPilot-Companion.py">Download
the companion</a></div></div>"""
    else:
        status = """<div class="card status"><span class="dot"></span><div>
<div class="head">Set up your PC once</div>
<p class="muted">Follow the steps below. After that you control everything from this page.</p></div></div>"""

    last_html = ""
    if last and not busy:
        dot = "ok" if last.get("ok") else "warn"
        what = "Practice run" if last.get("mode") == "practice" else "Real run"
        last_html = f"""<div class="card status"><span class="dot {dot}"></span><div>
<div class="head">Last: {what}{": " + esc(last.get("title") or "") if last.get("title") else ""}</div>
<p>{esc(last.get("summary") or "")}</p><p class="muted small">Finished {_since(last.get("finished"))}.</p>
</div></div>"""

    ready = online and not busy
    dis = "" if ready else " disabled"
    controls = ""
    if jobs:
        rows = []
        for j in jobs[:40]:
            score = j["fit_score"] or 0
            url = j["application_url"] or j["url"]
            who = company_for(j["company"], j["site"], j["application_url"])
            title = f'{j["title"]} at {who}' if who else j["title"]
            hidden = (f'<input type="hidden" name="url" value="{esc(url, quote=True)}">'
                      f'<input type="hidden" name="title" value="{esc(title, quote=True)}">')
            docs = []
            lk = 'style="color:var(--accent);font-weight:500"'
            if href := _doc_href(j["tailored_resume_path"]):
                docs.append(f'<a {lk} href="{esc(href, quote=True)}" target="_blank">Resume</a>')
            if href := _doc_href(j["cover_letter_path"]):
                docs.append(f'<a {lk} href="{esc(href, quote=True)}" target="_blank">Cover letter</a>')
            if rel := _rel(j["tailored_resume_path"]):
                docs.append(f'<a {lk} href="/edit?f={esc(rel, quote=True)}&amp;back=/autoapply">Edit resume</a>')
            if rel := _rel(j["cover_letter_path"]):
                docs.append(f'<a {lk} href="/edit?f={esc(rel, quote=True)}&amp;back=/autoapply">Edit cover letter</a>')
            flag = (' <span style="color:var(--warn)" title="The quality check flagged something: open Edit resume '
                    'to see what">&#9888; check resume</span>' if _review_note(j["tailored_resume_path"]) else "")
            skip = (f'<form method="post" action="/mark" style="display:inline"><input type="hidden" name="url" '
                    f'value="{esc(j["url"], quote=True)}"><button class="linkbtn" name="action" value="skip" '
                    f'title="Hide it and never auto-apply">&#10005; Not for me</button></form>')
            rows.append(f"""<div class="aa-job"><span class="score{" mid" if score < 7 else ""}">{score}</span>
<div class="t"><a href="{esc(j["url"] or url, quote=True)}" target="_blank" rel="noopener">{esc(j["title"])}</a>
<div class="meta">{job_meta(j)}</div>
<div class="muted small" style="margin-top:4px">{" &middot; ".join(docs)}{flag} &middot; {skip}</div></div>
<div class="aa-btns"><form method="post" action="/autoapply/start">{hidden}<input type="hidden" name="mode"
 value="practice"><button class="btn ghost sm"{dis}>Practice</button></form>
<form method="post" action="/autoapply/start" onsubmit="return confirm('Submit an application to this job under your name?')">
{hidden}<input type="hidden" name="mode" value="real"><input type="hidden" name="confirm" value="yes">
<button class="btn primary sm"{dis}>Apply</button></form></div></div>""")
        more = f'<p class="muted small" style="margin:10px 0 0">...and {len(jobs) - 40} more.</p>' if len(jobs) > 40 else ""
        acct = (f'<p class="muted small" style="margin:10px 0 0">{account_only} more need an account on the company&rsquo;s '
                f'site (Workday and similar), so they&rsquo;re left for you on <a href="/ready">Ready to apply</a>.</p>'
                if account_only else "")
        why_off = "" if ready else ('<p class="muted small" style="margin:0 0 10px">Buttons turn on when your PC is '
                                    'connected and nothing else is running.</p>')
        controls = f"""<div class="card">
<h2>Apply to your best matches</h2>{why_off}
<form method="post" action="/autoapply/start">
<input type="hidden" name="mode" value="real">
<div class="aa-real"><span>Submit</span><input type="number" name="count" min="1" max="{len(jobs)}"
 value="{min(3, len(jobs))}"><span>application(s), best match first</span></div>
<label class="aa-check"><input type="checkbox" name="confirm" value="yes" required>
<span>I understand these applications will be <b>submitted under my name</b>.</span></label>
<button class="btn primary"{dis}>Start applying</button>
<p class="muted small" style="margin:10px 0 0">Jobs it can&rsquo;t finish (an account, a CAPTCHA) don&rsquo;t count:
it skips them and keeps going until that many are submitted, or the list runs out. Each one takes a few minutes, and
every job it submits moves to the <a href="/applied">Applied</a> tab right away. Skipped jobs wait for you under
<a href="/ready?view=needs">Ready to apply &rarr; Needs you</a>. Press Stop any time.</p>
</form></div>
<div class="card"><h2>Or pick a job</h2>
<p class="muted small" style="margin:-4px 0 8px"><b>Practice</b> fills in the form on your PC and stops before
submitting, so you can check it. <b>Apply</b> submits it. These are the jobs auto-apply will work through, in
order: open <b>Resume</b> and <b>Cover letter</b> to see exactly what will be sent, fix anything with <b>Edit</b>,
or click <b>Not for me</b> to skip a job. (Ready to apply also lists jobs auto-apply can&rsquo;t do, like ones that
need an account, so the two lists differ.)</p>
<div class="aa-jobs">{"".join(rows)}</div>{more}{acct}</div>"""
    else:
        controls = """<div class="card empty"><div class="big">📭</div><h2>Nothing to auto-apply right now</h2>
<p class="muted">New matches show up here after each search.</p></div>"""

    setup_open = "" if online else " open"  # folded away only while the PC is connected
    body = f"""<h1>Auto-apply</h1>
<p class="sub">Claude fills in applications for you in Chrome on your Windows PC. You start and watch it here.</p>
<div class="stack">{status}{last_html}{controls}</div>
<details class="card"{setup_open}><summary><b>Set up your PC (one time)</b></summary>
<ol class="steps" style="margin-top:16px">
<li><b>Download the companion</b> <span class="muted">(skip this if you already have it):</span><br>
<a class="btn ghost sm" style="margin-top:8px" href="/companion.py" download="ApplyPilot-Companion.py">Download the
companion</a></li>
<li><b>Run it one last time from PowerShell</b> <span class="muted">(Start menu, type PowerShell):<br>
<code>cd ~\\Downloads</code><br><code>python ApplyPilot-Companion.py</code></span></li>
<li><b>Type Y</b> <span class="muted">when it asks <i>&ldquo;Let the website control auto-apply?&rdquo;</i>. A
small minimized <b>ApplyPilot Companion</b> window appears on the taskbar. That&rsquo;s it running; leave it open.</span></li>
<li><b>Come back here.</b> <span class="muted">Within a few seconds this page says your PC is connected. From now
on it starts by itself whenever Windows starts, and you never need PowerShell again.</span></li>
</ol></details>
<details class="card"><summary><b>Good to know</b></summary>
<ul class="muted small" style="margin:12px 0 0;padding-left:20px">
<li>It answers with your Setup details (salary, start date, work authorization, voluntary questions), so keep them
current.</li>
<li>Sites that need an account, a CAPTCHA, or an emailed code are left for you to finish by hand; the job card on
Ready to apply says why.</li>
<li>It uses your Claude subscription through Claude Code; each application is a few minutes of Claude&rsquo;s work.</li>
<li>Keep the PC awake while it runs, and don&rsquo;t use the Chrome window it opens.</li>
</ul></details>"""
    head = AUTOAPPLY_CSS
    if busy or not online:
        head += '<meta http-equiv="refresh" content="5">' if busy else '<meta http-equiv="refresh" content="15">'
    return page("Auto-apply", body, "auto", head)


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
            script = script.replace("__SERVER_URL__", f"https://{host}").replace("__SERVER_USER__", USER)
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
                back = _back(parse_qs(urlparse(self.path).query).get("back", [""])[0])
                return self._send(200, edit_html(doc, rel, back=back))
            return self._send(200, editor.to_docx(doc.read_text(encoding="utf-8"), doc.name.endswith("_CL.txt")),
                              "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                              {"Content-Disposition": f'attachment; filename="{doc.stem}.docx"'})
        if path == "/resume.txt":
            resume = DATA_DIR / "resume.txt"
            if resume.exists():
                return self._send(200, resume.read_bytes(), "text/plain; charset=utf-8")
        if path == "/setup":
            onboarded = parse_qs(urlparse(self.path).query).get("from") == ["onboard"]
            return self._send(200, page("ApplyPilot setup", setup_banner(onboarded)
                                        + setup_form.render(DATA_DIR, os.environ), "setup"))
        if path == "/onboard":
            body, refresh = onboarding.render(DATA_DIR, os.environ)
            head = onboarding.CSS + ('<meta http-equiv="refresh" content="4">' if refresh else "")
            return self._send(200, page("Build your master resume", body, "setup", head))
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
            view = parse_qs(urlparse(self.path).query).get("view", [""])[0]
            return self._send(200, ready_html(view))
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
        if path not in ("/run", "/setup", "/edit", "/mark", "/api/results", "/api/log", "/autoapply/start",
                        "/autoapply/stop", "/api/agent/poll", "/api/agent/status", "/api/agent/done",
                        "/onboard/start", "/onboard/answers", "/onboard/finish", "/onboard/restart",
                        "/onboard/retry"):
            return self._send(404, b"Not found", "text/plain")
        if not self._authorized():
            return
        if path.startswith("/autoapply/"):
            return self._post_autoapply(path)
        if path.startswith("/onboard/"):
            return self._post_onboard(path)
        if path.startswith("/api/agent/"):
            return self._post_agent(path)
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
            action = form.get("action", ["applied"])[0]
            if action in ("skip", "unskip"):
                set_skipped(url, action == "skip")
            else:
                mark_applied(url, action == "applied")
            referer = self.headers.get("Referer") or ""
            back = ("/applied" if "/applied" in referer else "/autoapply" if "/autoapply" in referer
                    else "/ready?view=needs" if "view=needs" in referer
                    else "/ready?view=hidden" if "view=hidden" in referer else "/ready")
            return self._send(303, b"", "text/plain", {"Location": back})
        if _run_lock.locked():
            msg = "Pipeline is already running."
        else:
            threading.Thread(target=run_pipeline, daemon=True).start()
            msg = "Pipeline started."
        self._send(303, msg.encode(), "text/plain", {"Location": "/status"})

    def _post_onboard(self, path: str):
        length = int(self.headers.get("Content-Length") or 0)
        if path == "/onboard/start":
            if length > 3 * setup_form.MAX_UPLOAD_BYTES + 1024 * 1024:
                return self._send(413, page("Too large", "<p>Those files are too large (10 MB each).</p>"))
            fields, files = setup_form.parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(length))
            sources, problems = onboarding.sources_from_upload(fields, files)
            if problems and not sources:
                body, _ = onboarding.render(DATA_DIR, os.environ, problems)
                return self._send(200, page("Build your master resume", body, "setup", onboarding.CSS))
            onboarding.start_draft(DATA_DIR, sources)
            log("Resume builder: drafting a master resume")
            return self._send(303, b"", "text/plain", {"Location": "/onboard"})
        form = parse_qs(self.rfile.read(min(length, 2 * 1024 * 1024)).decode("utf-8", errors="replace"),
                        keep_blank_values=True)
        one = {k: v[0] for k, v in form.items()}
        if path == "/onboard/restart":
            onboarding.restart(DATA_DIR)
        elif path == "/onboard/retry":
            onboarding.retry(DATA_DIR)
        elif path == "/onboard/answers":
            answers = [one.get(f"a{i}", "") for i in range(10)]
            onboarding.start_revise(DATA_DIR, [] if one.get("go") == "skip" else answers)
        elif path == "/onboard/finish":
            one["titles_picked"] = "\n".join(form.get("title_pick", []))
            problems = onboarding.finish(DATA_DIR, one)
            if problems:
                body, _ = onboarding.render(DATA_DIR, os.environ, problems)
                return self._send(200, page("Build your master resume", body, "setup", onboarding.CSS))
            log("Resume builder: master resume saved; Setup filled in")
            return self._send(303, b"", "text/plain", {"Location": "/setup?from=onboard"})
        return self._send(303, b"", "text/plain", {"Location": "/onboard"})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(min(length, 2 * 1024 * 1024)) or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _post_autoapply(self, path: str):
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(min(length, 65536)).decode("utf-8", errors="replace"))
        if path == "/autoapply/stop":
            control.stop()
            log("Auto-apply: stop requested from the website")
        else:
            mode = form.get("mode", ["practice"])[0]
            url = form.get("url", [""])[0].strip() or None
            title = form.get("title", [""])[0].strip()
            try:
                count = int(form.get("count", ["1"])[0])
            except ValueError:
                count = 1
            if mode == "real" and form.get("confirm", [""])[0] != "yes":
                pass  # real runs need the "submitted under my name" confirmation
            elif mode == "practice" and not url:
                pass
            else:
                if url:
                    count = 1
                elif not title:
                    title = f"{count} application{'s' if count != 1 else ''}"
                error = control.queue("real" if mode == "real" else "practice", url, count, title)
                log(f"Auto-apply: {error or f'queued {mode} run for {title}'}")
        return self._send(303, b"", "text/plain", {"Location": "/autoapply"})

    def _post_agent(self, path: str):
        data = self._read_json()
        if path == "/api/agent/poll":
            reply = control.poll(str(data.get("version", "")), bool(data.get("busy")))
            if reply.get("command"):
                log(f"Auto-apply: PC picked up {reply['command']['mode']} run")
        elif path == "/api/agent/status":
            control.status(str(data.get("message", "")))
            reply = {"ok": True}
        else:
            results = data.get("results") if isinstance(data.get("results"), list) else []
            mode = "real" if data.get("mode") == "real" else "practice"
            # A practice "applied" means the form was filled in, not submitted: only record what went wrong.
            record_results(results if mode == "real" else [r for r in results if r.get("status") != "applied"])
            summary, ok = run_summary(mode, results, bool(data.get("stopped")))
            if data.get("error"):
                summary, ok = str(data["error"])[:300], False
            control.finish(str(data.get("id", "")), summary, ok)
            log(f"Auto-apply finished: {summary}")
            reply = {"ok": True, "summary": summary}
        return self._send(200, json.dumps(reply).encode(), "application/json")

    def _post_edit(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2 * 1024 * 1024:
            return self._send(413, b"Too large", "text/plain")
        form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"), keep_blank_values=True)
        rel = form.get("f", [""])[0]
        doc = editor.resolve(DATA_DIR, rel)
        if not doc:
            return self._send(404, b"Not found", "text/plain")
        back = _back(form.get("back", [""])[0])
        if form.get("action", ["save"])[0] == "checked":
            editor.mark_checked(doc)
            return self._send(303, b"", "text/plain", {"Location": back})
        content = form.get("content", [""])[0]
        if not content.strip():
            return self._send(200, edit_html(doc, rel, "Nothing saved: the text was empty.", back))
        problem = editor.save(doc, content)
        editor.mark_checked(doc)
        log(f"Edited {rel}")
        return self._send(200, edit_html(doc, rel, problem or "Saved. The PDF has been rebuilt with your changes.",
                                         back))

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


def relayout_cover_letters() -> None:
    """Once: re-render existing cover letter PDFs in the letter layout (they used the resume layout before)."""
    marker = DATA_DIR / ".cl_layout_v1"
    letters = sorted((DATA_DIR / "cover_letters").glob("*_CL.txt"))
    if marker.exists() or not letters:
        marker.touch()
        return
    try:
        from playwright.sync_api import sync_playwright

        from applypilot.scoring.pdf import build_letter_html
        done = 0
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            for txt in letters:
                try:
                    page.set_content(build_letter_html(txt.read_text(encoding="utf-8")), wait_until="networkidle")
                    page.pdf(path=str(txt.with_suffix(".pdf")), format="Letter", print_background=True,
                             margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
                    done += 1
                except Exception as e:  # one bad file shouldn't stop the rest
                    log(f"Cover letter re-layout failed for {txt.name}: {e}")
            browser.close()
        marker.touch()
        log(f"Re-laid out {done} cover letter PDF(s) as letters")
    except Exception as e:
        log(f"Cover letter re-layout skipped: {e}")


def main() -> None:
    if (DATA_DIR / "applypilot.db").exists():  # bring the job table up to date (e.g. the company column)
        from applypilot.database import init_db
        init_db()
    problems = seed_config()
    state["config_problems"] = problems
    for p in problems:
        log(f"Config: {p}")
    if PASSWORD:
        log(f"Dashboard enabled (user '{USER}')")
    else:
        log("DASHBOARD_PASSWORD not set: web UI is locked; only /health responds.")

    threading.Thread(target=relayout_cover_letters, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Listening on :{PORT} (data dir {DATA_DIR}, interval {INTERVAL_HOURS}h)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
