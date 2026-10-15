"""ApplyPilot Companion: auto-apply from your own PC using jobs prepared by your ApplyPilot server.

Run it with:  python ApplyPilot-Companion.py

What it does:
  1. Downloads the jobs on your "Ready to apply" page, with their tailored resumes and cover letters.
  2. Uses ApplyPilot's auto-apply (Claude Code driving Chrome on this PC) to fill in applications.
     A practice run fills in the form but never submits.
  3. Tells the server which jobs were really submitted, so they're marked Applied and never sent twice.

Needs: Python 3.11+, Google Chrome, Node.js (for npx), and Claude Code installed and signed in.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

VERSION = "5"
SERVER = "__SERVER_URL__"  # filled in by the server when you download this file
USER = "applypilot"
MODEL = os.environ.get("APPLYPILOT_MODEL", "sonnet")  # Claude model used to fill in forms

HOME = Path.home() / "ApplyPilotCompanion"
CONFIG = HOME / "config.json"
DATA = HOME / "data"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def say(msg: str = "") -> None:
    print(msg, flush=True)


def ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(f"{prompt} ").strip()
    except EOFError:
        answer = ""
    return answer or default


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def request(path: str, password: str, data: bytes | None = None, timeout: int = 300) -> bytes:
    req = urllib.request.Request(SERVER.rstrip("/") + path, data=data)
    token = base64.b64encode(f"{USER}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Setup checks
# ---------------------------------------------------------------------------

def get_password(cfg: dict) -> str:
    password = cfg.get("password", "")
    while True:
        if not password:
            password = getpass.getpass("Your ApplyPilot site password (it won't show as you type): ").strip()
        try:
            request("/status.json", password, timeout=30)
            cfg["password"] = password
            save_config(cfg)
            return password
        except urllib.error.HTTPError as e:
            if e.code == 401:
                say("That password didn't work. Try again.")
                password = ""
                continue
            raise


def ensure_installed(cfg: dict, password: str) -> None:
    """Install (or update) the same ApplyPilot version your server runs."""
    say("Checking ApplyPilot is installed and up to date...")
    package = request("/api/package.tar.gz", password)
    digest = hashlib.sha256(package).hexdigest()
    importable = subprocess.run([sys.executable, "-c", "import applypilot"], capture_output=True, check=False).returncode == 0
    if importable and cfg.get("package_sha") == digest:
        return
    HOME.mkdir(parents=True, exist_ok=True)
    tarball = HOME / "applypilot-server.tar.gz"
    tarball.write_bytes(package)
    say("Installing ApplyPilot (first time, or after an update). This can take a few minutes...")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", str(tarball)], check=False)
    if result.returncode != 0:
        sys.exit("Installing ApplyPilot failed. See the messages above.")
    cfg["package_sha"] = digest
    save_config(cfg)


def self_update(password: str) -> None:
    """Replace this file with the server's latest companion and restart it, if they differ."""
    if os.environ.get("APPLYPILOT_COMPANION_UPDATED"):
        return
    try:
        latest = request("/companion.py", password, timeout=60)
    except (urllib.error.URLError, OSError):
        return
    me = Path(__file__).resolve()
    if latest.strip() and latest != me.read_bytes():
        say("Updating the companion to the latest version...")
        me.write_bytes(latest)
        env = dict(os.environ, APPLYPILOT_COMPANION_UPDATED="1")
        sys.exit(subprocess.run([sys.executable, str(me), *sys.argv[1:]], env=env, check=False).returncode)


def check_tools() -> None:
    problems = []
    if not shutil.which("claude"):
        problems.append("Claude Code isn't on your PATH. Install it from https://claude.ai/code and sign in.")
    if not shutil.which("npx"):
        problems.append("Node.js (npx) isn't on your PATH. Install it from https://nodejs.org.")
    from applypilot.config import get_chrome_path
    try:
        get_chrome_path()
    except FileNotFoundError:
        problems.append("Google Chrome wasn't found. Install it, or set CHROME_PATH to chrome.exe.")
    if problems:
        say("\nSome things are missing:")
        for p in problems:
            say(f"  - {p}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Jobs: download, local queue, results
# ---------------------------------------------------------------------------

def download_jobs(password: str) -> dict:
    say("Downloading your Ready to apply jobs...")
    bundle = zipfile.ZipFile(io.BytesIO(request("/api/bundle.zip", password)))
    manifest = json.loads(bundle.read("manifest.json"))
    for folder in ("tailored_resumes", "cover_letters"):
        shutil.rmtree(DATA / folder, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)
    for name in bundle.namelist():
        if name == "manifest.json" or name.startswith("/") or ".." in name:
            continue
        target = DATA / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bundle.read(name))
    return manifest


def build_queue(manifest: dict) -> None:
    """A fresh local job list, so practice runs never leave anything marked as applied."""
    from applypilot.database import get_connection, init_db
    init_db()
    conn = get_connection()
    conn.execute("DELETE FROM jobs")  # emptied, not deleted: Windows won't delete a file that's still open
    for j in manifest["jobs"]:
        conn.execute(
            "INSERT OR REPLACE INTO jobs (url, title, site, location, application_url, fit_score, "
            "full_description, tailored_resume_path, cover_letter_path, apply_attempts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (j["url"], j["title"], j["site"], j["location"], j["application_url"], j["fit_score"],
             j["full_description"],
             str(DATA / j["tailored_resume"]) if j.get("tailored_resume") else None,
             str(DATA / j["cover_letter"]) if j.get("cover_letter") else None,
             j.get("apply_attempts") or 0))
    conn.commit()


def run_results() -> list[dict]:
    """What happened to each job in the last real run (the queue starts empty of results)."""
    from applypilot.database import get_connection
    rows = get_connection().execute(
        "SELECT url, apply_status, apply_error, applied_at FROM jobs "
        "WHERE apply_status IS NOT NULL AND apply_status != 'in_progress'").fetchall()
    return [{"url": r[0], "status": r[1], "error": r[2], "at": r[3]} for r in rows]


def report(password: str, results: list[dict]) -> None:
    if not results:
        return
    body = json.dumps({"results": results}).encode()
    for attempt in range(3):
        try:
            reply = json.loads(request("/api/results", password, data=body, timeout=60))
            applied = sum(1 for r in results if r["status"] == "applied")
            say(f"Sent results to your server: {applied} applied, {len(results) - applied} not finished "
                f"({reply.get('updated', 0)} updated).")
            return
        except (urllib.error.URLError, ValueError) as e:
            say(f"Couldn't reach the server ({e}); retrying...")
    backup = HOME / "unsent-results.json"
    backup.write_text(json.dumps(results, indent=2), encoding="utf-8")
    say(f"Couldn't send results. They're saved in {backup}; tick 'I applied' on the site for those jobs.")


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def show_jobs(manifest: dict, min_score: int) -> list[dict]:
    jobs = [j for j in manifest["jobs"] if (j["fit_score"] or 0) >= min_score]
    say(f"\n{len(jobs)} job(s) auto-apply can handle (score {min_score}+). Best matches:")
    for i, j in enumerate(jobs[:10], 1):
        say(f"  {i:2}. [{j['fit_score']}] {j['title'][:55]}  ({j['site']})")
    if len(jobs) > 10:
        say(f"  ...and {len(jobs) - 10} more")
    skipped = manifest.get("needs_account", 0)
    if skipped:
        say(f"  ({skipped} more need an account on the company's site; apply to those by hand.)")
    return jobs


def apply_now(limit: int, min_score: int, practice: bool, url: str | None = None,
              password: str = "") -> None:
    from applypilot.apply.launcher import main as apply_main
    started = time.time()
    stop = threading.Event()
    if not practice:  # move each submitted job to the site's Applied tab as soon as it's done
        threading.Thread(target=_report_as_you_go, args=(password, stop), daemon=True).start()
    try:
        apply_main(limit=limit, target_url=url, min_score=min_score, model=MODEL, dry_run=practice, workers=1)
    finally:
        stop.set()
        send_logs(password, started, practice)


def _report_as_you_go(password: str, stop: threading.Event) -> None:
    sent: set[str] = set()
    while not stop.wait(15):
        try:
            new = [r for r in run_results() if r["status"] == "applied" and r["url"] not in sent]
            if new:
                request("/api/results", password, data=json.dumps({"results": new}).encode(), timeout=60)
                sent.update(r["url"] for r in new)
                say(f"  Marked {len(new)} job(s) as applied on your site.")
        except Exception:  # noqa: BLE001 - the full report at the end covers anything missed here
            pass


def send_logs(password: str, since: float, practice: bool) -> None:
    """Share this run's notes with your server so problems can be diagnosed from there."""
    from applypilot.config import LOG_DIR as logs
    files = sorted((f for f in logs.glob("claude_*.txt") if f.stat().st_mtime >= since - 5),
                   key=lambda f: f.stat().st_mtime) if logs.exists() else []
    parts = [f"mode: {'practice' if practice else 'real'}\nmodel: {MODEL}\n"]
    worker = logs / "worker-0.log"
    if worker.exists():
        parts.append("=== worker-0.log (end) ===\n" + worker.read_text(encoding="utf-8", errors="replace")[-20000:])
    for f in files[-5:]:
        parts.append(f"=== {f.name} ===\n" + f.read_text(encoding="utf-8", errors="replace")[-200000:])
    try:
        request("/api/log", password, data="\n\n".join(parts).encode("utf-8"), timeout=60)
    except (urllib.error.URLError, OSError):
        pass


def main() -> None:
    if sys.version_info < (3, 11):  # noqa: UP036 - people may run an older Python
        sys.exit("ApplyPilot needs Python 3.11 or newer.")
    if "__SERVER" in SERVER:
        sys.exit("Download this file from your ApplyPilot site's Auto-apply page; it fills in your server address.")
    os.environ["APPLYPILOT_DIR"] = str(DATA)  # before importing applypilot, which reads it at import time
    DATA.mkdir(parents=True, exist_ok=True)

    say(f"ApplyPilot Companion (version {VERSION})\n" + "=" * 32)
    cfg = load_config()
    password = get_password(cfg)
    self_update(password)
    ensure_installed(cfg, password)
    check_tools()

    manifest = download_jobs(password)
    min_score = int(manifest.get("min_score") or 6)
    while True:
        build_queue(manifest)
        jobs = show_jobs(manifest, min_score)
        if not jobs:
            say("\nNothing to apply to right now. Run this again after the next search.")
            return
        say("\nWhat would you like to do?")
        say("  1  Practice run on a job you pick (fills in the form, does NOT submit)")
        say("  2  Apply for real")
        say("  3  Refresh the job list from the server")
        say("  q  Quit")
        choice = ask("Choose 1, 2, 3 or q:", "q").lower()
        if choice == "1":
            pick = ask(f"Which job should it practice on? (1-{min(10, len(jobs))}, default 1):", "1")
            job = jobs[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= min(10, len(jobs)) else jobs[0]
            say(f"\nOpening Chrome for: {job['title']} ({job['site']}). Nothing will be submitted.")
            apply_now(1, min_score, practice=True, url=job["application_url"] or job["url"], password=password)
            say("\nPractice run finished. Nothing was submitted or marked as applied.")
            # Share only problems (e.g. the site needs an account); a practice "applied" means nothing.
            report(password, [r for r in run_results() if r["status"] != "applied"])
            manifest = download_jobs(password)
        elif choice == "2":
            n = ask(f"How many jobs should it apply to? (1-{len(jobs)}, default 1):", "1")
            n = max(1, min(len(jobs), int(n))) if n.isdigit() else 1
            confirm = ask(f"This will SUBMIT up to {n} application(s) under your name. Type YES to go ahead:")
            if confirm != "YES":
                say("Cancelled.")
                continue
            apply_now(n, min_score, practice=False, password=password)
            report(password, run_results())
            manifest = download_jobs(password)
        elif choice == "3":
            manifest = download_jobs(password)
        else:
            say("Bye.")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped.")
