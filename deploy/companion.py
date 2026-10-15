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
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

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
    db = DATA / "applypilot.db"
    if db.exists():
        db.unlink()
    init_db()
    conn = get_connection()
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
    say(f"\n{len(jobs)} job(s) ready to apply (score {min_score}+). Best matches:")
    for j in jobs[:8]:
        say(f"  {j['fit_score']}  {j['title'][:60]}  ({j['site']})")
    if len(jobs) > 8:
        say(f"  ...and {len(jobs) - 8} more")
    return jobs


def apply_now(limit: int, min_score: int, practice: bool) -> None:
    from applypilot.apply.launcher import main as apply_main
    apply_main(limit=limit, min_score=min_score, model=MODEL, dry_run=practice, workers=1)


def main() -> None:
    if sys.version_info < (3, 11):  # noqa: UP036 - people may run an older Python
        sys.exit("ApplyPilot needs Python 3.11 or newer.")
    if "__SERVER" in SERVER:
        sys.exit("Download this file from your ApplyPilot site's Auto-apply page; it fills in your server address.")
    os.environ["APPLYPILOT_DIR"] = str(DATA)  # before importing applypilot, which reads it at import time
    DATA.mkdir(parents=True, exist_ok=True)

    say("ApplyPilot Companion\n" + "=" * 20)
    cfg = load_config()
    password = get_password(cfg)
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
        say("  1  Practice run on the top job (fills in the form, does NOT submit)")
        say("  2  Apply for real")
        say("  3  Refresh the job list from the server")
        say("  q  Quit")
        choice = ask("Choose 1, 2, 3 or q:", "q").lower()
        if choice == "1":
            say("\nOpening Chrome. Watch the form get filled in; nothing will be submitted.")
            apply_now(1, min_score, practice=True)
            say("\nPractice run finished. Nothing was submitted or marked as applied.")
        elif choice == "2":
            n = ask(f"How many jobs should it apply to? (1-{len(jobs)}, default 1):", "1")
            n = max(1, min(len(jobs), int(n))) if n.isdigit() else 1
            confirm = ask(f"This will SUBMIT up to {n} application(s) under your name. Type YES to go ahead:")
            if confirm != "YES":
                say("Cancelled.")
                continue
            apply_now(n, min_score, practice=False)
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
