"""ApplyPilot Companion: auto-apply from your own PC using jobs prepared by your ApplyPilot server.

Run it with:  python ApplyPilot-Companion.py
Then say yes to "Let the website control auto-apply?" and use the Auto-apply tab from then on: the companion
keeps running in a small minimized window and starts by itself with Windows.

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
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

VERSION = "7"
SERVER = "__SERVER_URL__"  # filled in by the server when you download this file
USER = "applypilot"
MODEL = os.environ.get("APPLYPILOT_MODEL", "sonnet")  # Claude model used to fill in forms

HOME = Path.home() / "ApplyPilotCompanion"
CONFIG = HOME / "config.json"
DATA = HOME / "data"
AGENT_PORT = 47831  # held while the background companion runs, so only one copy works at a time


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def say(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # e.g. a job title with characters the Windows console can't show
        enc = sys.stdout.encoding or "ascii"
        print(msg.encode(enc, errors="replace").decode(enc), flush=True)


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


def ensure_browser_tool() -> None:
    """Install Claude's browser tool once, so each job starts in seconds instead of fetching it via npx."""
    from applypilot.apply.launcher import PLAYWRIGHT_MCP_VERSION as version
    prefix = HOME / "node"
    pkg = prefix / "node_modules" / "@playwright" / "mcp"
    try:
        current = json.loads((pkg / "package.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        current = None
    if current != version:
        say("Installing the browser tool (one time)...")
        npm = ["cmd", "/c", "npm"] if os.name == "nt" else ["npm"]
        result = subprocess.run([*npm, "install", "--prefix", str(prefix), "--no-audit", "--no-fund",
                                 f"@playwright/mcp@{version}"], check=False)
        if result.returncode != 0:
            say("Couldn't install it; jobs will fetch it each time instead (slower, but works).")
            return
    os.environ["APPLYPILOT_PLAYWRIGHT_MCP"] = str(pkg / "cli.js")


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


def tool_problems() -> list[str]:
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
    return problems


def check_tools() -> None:
    problems = tool_problems()
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
# Background mode: the website starts runs, this PC does them
# ---------------------------------------------------------------------------

def agent_lock() -> socket.socket | None:
    """Claim the 'background companion is running' flag. None if another copy already has it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", AGENT_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def agent_running() -> bool:
    lock = agent_lock()
    if lock:
        lock.close()
        return False
    return True


def _start_menu() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def install_background() -> bool:
    """Run the companion minimized in the background now and whenever Windows starts."""
    if os.name != "nt":
        say("Background mode is set up for Windows. On this computer, run:  python ApplyPilot-Companion.py --agent")
        return False
    HOME.mkdir(parents=True, exist_ok=True)
    me = Path(__file__).resolve()
    target = HOME / "ApplyPilot-Companion.py"  # a stable home, so cleaning out Downloads doesn't break it
    if me != target:
        shutil.copyfile(me, target)
    starter = HOME / "Start ApplyPilot Companion.cmd"
    starter.write_text(f'@echo off\nstart "ApplyPilot Companion" /min "{sys.executable}" "{target}" --agent\n',
                       encoding="utf-8")
    # Shortcuts: one in Startup (runs at login), one in the Start menu (so you can search for it).
    for link in (_start_menu() / "Startup" / "ApplyPilot Companion.lnk", _start_menu() / "ApplyPilot Companion.lnk"):
        link.parent.mkdir(parents=True, exist_ok=True)
        script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
                  f"$s.TargetPath='{starter}';$s.WorkingDirectory='{HOME}';$s.WindowStyle=7;$s.Save()")
        made = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, check=False)
        if made.returncode != 0 or not link.exists():
            shutil.copyfile(starter, link.with_suffix(".cmd"))
    if "WindowsApps" in sys.executable:
        say(f"Note: if it doesn't start with Windows, double-click {starter}")
    subprocess.Popen(["cmd", "/c", str(starter)], env={k: v for k, v in os.environ.items()
                                                       if k != "APPLYPILOT_COMPANION_UPDATED"})
    return True


def background_installed() -> bool:
    startup = _start_menu() / "Startup"
    return os.name == "nt" and any((startup / f"ApplyPilot Companion{ext}").exists() for ext in (".lnk", ".cmd"))


def offer_background(host: str) -> bool:
    say("\nRecommended: let the website control auto-apply. The companion then runs quietly in a minimized")
    say("window, starts by itself with Windows, and you start practice runs or real applications from the")
    say(f"Auto-apply tab at {host}/autoapply. No more PowerShell.")
    if ask("Let the website control auto-apply? (Y/n):", "y").lower().startswith("y") and install_background():
        say("\nDone. A minimized 'ApplyPilot Companion' window is now on your taskbar; leave it open.")
        say(f"Go to {host}/autoapply. It shows your PC as connected within a few seconds.")
        say("You can close this PowerShell window.")
        return True
    return False


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.kill()


def _agent_post(path: str, password: str, payload: dict, timeout: int = 30) -> dict:
    return json.loads(request(path, password, data=json.dumps(payload).encode(), timeout=timeout) or b"{}")


def _newer_companion(password: str) -> bytes | None:
    try:
        latest = request("/companion.py", password, timeout=60)
    except (urllib.error.URLError, OSError):
        return None
    return latest if latest.strip() and latest != Path(__file__).resolve().read_bytes() else None


def run_command(cmd: dict, cfg: dict, password: str) -> None:
    """Do one run the website asked for, in a child process so Stop can end it (and its Chrome) cleanly."""
    mode = cmd.get("mode", "practice")
    say(f"\n[{time.strftime('%H:%M')}] Website asked for a {mode} run: {cmd.get('title') or ''}")
    payload = {"id": cmd.get("id"), "mode": mode, "results": [], "stopped": False}
    try:
        post_status(password, "Checking ApplyPilot is up to date on your PC...")
        ensure_installed(cfg, password)
    except SystemExit as e:
        payload["error"] = f"Couldn't update ApplyPilot on your PC: {e.code}"
        _agent_post("/api/agent/done", password, payload)
        return
    db = DATA / "applypilot.db"
    if db.exists():  # this run's outcomes start from a clean slate
        conn = sqlite3.connect(db, timeout=30)
        conn.execute("UPDATE jobs SET apply_status = NULL, apply_error = NULL")
        conn.commit()
        conn.close()
    env = dict(os.environ, APPLYPILOT_DIR=str(DATA), APPLYPILOT_COMPANION_UPDATED="1")
    extra = {} if os.name == "nt" else {"start_new_session": True}
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--run", json.dumps(cmd)],
                             env=env, **extra)
    while child.poll() is None:
        try:
            if _agent_post("/api/agent/poll", password, {"version": VERSION, "busy": True}).get("stop"):
                say("Stop pressed on the website; stopping...")
                _kill_tree(child)
                payload["stopped"] = True
                break
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(3)
    child.wait()
    if child.returncode == 3:
        payload["error"] = (DATA / "problem.txt").read_text(encoding="utf-8") if (DATA / "problem.txt").exists() \
            else "Something needed on your PC is missing."
    payload["results"] = local_results()
    for attempt in range(5):
        try:
            reply = _agent_post("/api/agent/done", password, payload, timeout=60)
            say(f"Finished: {reply.get('summary', '')}")
            return
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(5 * (attempt + 1))
    backup = HOME / "unsent-results.json"
    backup.write_text(json.dumps(payload["results"], indent=2), encoding="utf-8")


def run_child(cmd: dict) -> int:
    """The work itself (runs as a child of the background companion)."""
    cfg = load_config()
    password = cfg.get("password", "")
    problems = tool_problems()
    (DATA / "problem.txt").unlink(missing_ok=True)
    if problems:
        (DATA / "problem.txt").write_text(" ".join(problems), encoding="utf-8")
        return 3
    ensure_browser_tool()
    post_status(password, "Downloading your jobs and resumes...")
    manifest = download_jobs(password)
    build_queue(manifest)
    min_score = int(manifest.get("min_score") or 6)
    practice = cmd.get("mode") != "real"
    url = cmd.get("url")
    if url and not any(url in (j.get("application_url"), j.get("url")) for j in manifest["jobs"]):
        post_status(password, "That job is no longer on the auto-apply list.")
        return 0
    post_status(password, "Opening Chrome...")
    apply_now(1 if url else int(cmd.get("count") or 1), min_score, practice=practice, url=url, password=password)
    return 0


def agent_main() -> None:
    lock = agent_lock()
    if not lock:
        say("The ApplyPilot companion is already running in the background. Use the Auto-apply tab on your site.")
        time.sleep(8)
        return
    cfg = load_config()
    password = cfg.get("password", "")
    if not password:
        say("Run 'python ApplyPilot-Companion.py' once first, to enter your site password.")
        time.sleep(30)
        return
    say(f"ApplyPilot Companion (version {VERSION}), running in the background.")
    say(f"Start and watch applications at {SERVER}/autoapply")
    say("Leave this window open; minimized is fine.\n")
    last_update_check = time.time()
    offline_since = None
    while True:
        try:
            reply = _agent_post("/api/agent/poll", password, {"version": VERSION, "busy": False})
            if offline_since:
                say(f"[{time.strftime('%H:%M')}] Connected to your site again.")
            offline_since = None
        except urllib.error.HTTPError as e:
            if e.code == 401:
                say("Your site password changed. Run 'python ApplyPilot-Companion.py' to enter the new one.")
                time.sleep(60)
                return
            reply = {}
        except (urllib.error.URLError, OSError, ValueError):
            if not offline_since:
                offline_since = time.time()
                say(f"[{time.strftime('%H:%M')}] Can't reach your site right now; will keep trying.")
            time.sleep(15)
            continue
        if reply.get("command"):
            try:
                run_command(reply["command"], cfg, password)
            except Exception as e:  # noqa: BLE001 - keep the background companion alive
                say(f"That run hit a problem: {e}")
                try:
                    _agent_post("/api/agent/done", password, {"id": reply["command"].get("id"),
                                                              "mode": reply["command"].get("mode"), "results": [],
                                                              "error": f"The companion hit a problem: {e}"})
                except (urllib.error.URLError, OSError, ValueError):
                    pass
            continue
        if time.time() - last_update_check > 300:  # pick up companion updates while idle
            last_update_check = time.time()
            latest = _newer_companion(password)
            if latest:
                say("Updating the companion and restarting...")
                Path(__file__).resolve().write_bytes(latest)
                lock.close()
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--agent"])
                return
        time.sleep(4)


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
    # Live progress for the website; real runs also move each submitted job to the Applied tab right away.
    threading.Thread(target=_report_as_you_go, args=(password, stop, practice), daemon=True).start()
    try:
        apply_main(limit=limit, target_url=url, min_score=min_score, model=MODEL, dry_run=practice, workers=1)
    finally:
        stop.set()
        send_logs(password, started, practice)


def _report_as_you_go(password: str, stop: threading.Event, practice: bool) -> None:
    sent: set[str] = set()
    shown = ""
    while not stop.wait(5):
        try:
            current = local_results(in_progress=True)
            if current:
                j = current[0]
                verb = "Practicing on" if practice else "Applying to"
                msg = f"{verb}: {j['title']}" + (f" at {j['site']}" if j.get("site") else "")
                if msg != shown:
                    post_status(password, msg)
                    shown = msg
            if practice:
                continue
            new = [r for r in local_results() if r["status"] == "applied" and r["url"] not in sent]
            if new:
                request("/api/results", password, data=json.dumps({"results": new}).encode(), timeout=60)
                sent.update(r["url"] for r in new)
                say(f"  Marked {len(new)} job(s) as applied on your site.")
        except Exception:  # noqa: BLE001 - the full report at the end covers anything missed here
            pass


def local_results(in_progress: bool = False) -> list[dict]:
    """This run's outcomes (or the job being worked on), read straight from the local job list."""
    db = DATA / "applypilot.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(db, timeout=30)
    try:
        where = "apply_status = 'in_progress'" if in_progress else \
            "apply_status IS NOT NULL AND apply_status != 'in_progress'"
        rows = conn.execute(f"SELECT url, apply_status, apply_error, applied_at, title, site FROM jobs WHERE {where}")
        return [{"url": r[0], "status": r[1], "error": r[2], "at": r[3], "title": r[4], "site": r[5]} for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def post_status(password: str, message: str) -> None:
    try:
        request("/api/agent/status", password, data=json.dumps({"message": message}).encode(), timeout=30)
    except (urllib.error.URLError, OSError):
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

    if "--agent" in sys.argv:
        return agent_main()
    if "--run" in sys.argv:
        sys.exit(run_child(json.loads(sys.argv[sys.argv.index("--run") + 1])))

    say(f"ApplyPilot Companion (version {VERSION})\n" + "=" * 32)
    cfg = load_config()
    password = get_password(cfg)
    self_update(password)
    ensure_installed(cfg, password)
    check_tools()
    ensure_browser_tool()
    if agent_running():
        say(f"\nThe companion is already running in the background. Use the Auto-apply tab: {SERVER}/autoapply")
        return
    if not background_installed() and offer_background(SERVER):
        return

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
        say("  4  Let the website control auto-apply (runs in the background)")
        say("  q  Quit")
        choice = ask("Choose 1, 2, 3, 4 or q:", "q").lower()
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
        elif choice == "4":
            if offer_background(SERVER):
                return
        else:
            say("Bye.")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped.")
