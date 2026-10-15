"""Remote control for the companion on the user's PC.

The website queues a run (practice or real); the companion, running in the background on the PC, polls for it,
does the work, posts what it's doing, and reports when it's finished. One run at a time.
State lives in a small JSON file so a server restart doesn't lose track of a run.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

ONLINE_SECONDS = 45  # the companion polls every few seconds; this long without one means it's not running

_lock = threading.Lock()


class Control:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, st: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def snapshot(self) -> dict:
        with _lock:
            st = self._load()
        agent = st.get("agent") or {}
        st["online"] = bool(agent.get("last_seen")) and time.time() - agent["last_seen"] < ONLINE_SECONDS
        return st

    def queue(self, mode: str, url: str | None, count: int, title: str) -> str | None:
        """Ask the PC to start a run. Returns an error message, or None if it was queued."""
        with _lock:
            st = self._load()
            if st.get("pending") or st.get("running"):
                return "Auto-apply is already busy. Wait for it to finish, or press Stop."
            st["pending"] = {"id": uuid.uuid4().hex[:12], "mode": mode, "url": url or None,
                             "count": max(1, count), "title": title, "created": time.time()}
            self._save(st)
        return None

    def stop(self) -> None:
        with _lock:
            st = self._load()
            st["pending"] = None
            if st.get("running"):
                st["running"]["stop"] = True
                st["running"]["message"] = "Stopping..."
            self._save(st)

    def poll(self, version: str, busy: bool) -> dict:
        """The companion checking in. Hands it the next run when it's free, or tells it to stop."""
        with _lock:
            st = self._load()
            st["agent"] = {"last_seen": time.time(), "version": version, "busy": busy}
            running = st.get("running")
            reply: dict = {}
            if busy:
                reply["stop"] = bool(running and running.get("stop"))
            else:
                if running:  # the PC is free but never said it finished (it restarted, or the window was closed)
                    st["last"] = {**running, "finished": time.time(), "ok": False,
                                  "summary": "Stopped before finishing (the companion was closed or the PC restarted)."}
                    st["running"] = None
                if st.get("pending"):
                    cmd = st["pending"]
                    st["running"] = {**cmd, "started": time.time(), "updated": time.time(),
                                     "message": "Starting up on your PC...", "stop": False}
                    st["pending"] = None
                    reply["command"] = cmd
            self._save(st)
        return reply

    def status(self, message: str) -> None:
        with _lock:
            st = self._load()
            running = st.get("running")
            if running and not running.get("stop"):
                running["message"] = message[:300]
                running["updated"] = time.time()
                self._save(st)

    def finish(self, run_id: str, summary: str, ok: bool) -> None:
        with _lock:
            st = self._load()
            running = st.get("running") or {}
            if running.get("id") == run_id:
                st["last"] = {**running, "finished": time.time(), "ok": ok, "summary": summary}
                st["running"] = None
                self._save(st)
