"""Short summaries of the jobs applied to, and the pay a posting mentions, for the Applied spreadsheet.

Summaries are written by the AI service chosen in Setup, a few at a time in the background, and saved in
the job database (job_summary). Until one is written, or when the AI can't be reached, the opening of the
posting is shown instead.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

PROMPT = """In one or two plain sentences (at most 40 words), say what this job is: the role, what the person
would mainly do, and what the company does. No praise, no bullet points, no preamble.

Job title: {title}
Company: {company}

Posting:
{description}"""

_MONEY = r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d\d)?|\$\s?\d{2,3}(?:\.\d)?\s?[kK]"
_PAY = re.compile(rf"({_MONEY})(?:\s*(?:-|–|—|to)\s*({_MONEY}))?(\s*(?:/|per|an|a)\s*(?:year|yr|hour|hr|annum))?",
                  re.I)
_BOILERPLATE = re.compile(r"^(about (us|the (company|role|team))|who we are|overview|job (description|summary)|"
                          r"the role|position summary)\s*:?\s*$", re.I)


def pay_in(text: str | None) -> str:
    """The first pay range a posting mentions ('$150,000 – $180,000'), or ''."""
    m = _PAY.search(text or "")
    if not m:
        return ""
    low, high, per = m.group(1), m.group(2), (m.group(3) or "").strip()
    out = f"{low.replace(' ', '')} – {high.replace(' ', '')}" if high else low.replace(" ", "")
    return f"{out} {per}".strip()


def opening(description: str | None, limit: int = 220) -> str:
    """The first real sentences of a posting, skipping headings like 'About us'."""
    lines = [ln.strip(" -•*#\t") for ln in (description or "").splitlines()]
    text = " ".join(ln for ln in lines if ln and not _BOILERPLATE.match(ln))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + "…"


def ensure_column(conn: sqlite3.Connection) -> None:
    if "job_summary" not in {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}:
        conn.execute("ALTER TABLE jobs ADD COLUMN job_summary TEXT")
        conn.commit()


def _client(env: dict[str, str]):
    """An AI client for the provider saved in Setup, or None if there isn't one."""
    from applypilot.llm import LLMClient
    if env.get("LLM_URL"):
        return LLMClient(env["LLM_URL"].rstrip("/"), env.get("LLM_MODEL") or "local-model", env.get("LLM_API_KEY", ""))
    if env.get("GEMINI_API_KEY"):
        return LLMClient("https://generativelanguage.googleapis.com/v1beta/openai",
                         env.get("LLM_MODEL") or "gemini-3.8-flash", env["GEMINI_API_KEY"])
    if env.get("OPENAI_API_KEY"):
        return LLMClient("https://api.openai.com/v1", env.get("LLM_MODEL") or "gpt-4o-mini", env["OPENAI_API_KEY"])
    return None


def fill(db: Path, env: dict[str, str], limit: int = 20) -> int:
    """Write summaries for applied jobs that don't have one yet. Returns how many were written."""
    if not db.exists():
        return 0
    conn = sqlite3.connect(db, timeout=30)
    try:
        ensure_column(conn)
        rows = conn.execute(
            "SELECT url, title, company, full_description FROM jobs WHERE applied_at IS NOT NULL "
            "AND job_summary IS NULL AND full_description IS NOT NULL ORDER BY applied_at DESC LIMIT ?",
            (limit,)).fetchall()
        if not rows:
            return 0
        client = _client(env)
        if client is None:
            return 0
        written = 0
        for url, title, company, description in rows:
            text = client.chat([{"role": "user", "content": PROMPT.format(
                title=title or "", company=company or "", description=(description or "")[:6000])}],
                max_tokens=160, temperature=0.2)
            summary = re.sub(r"\s+", " ", text or "").strip().strip('"')
            if summary:
                conn.execute("UPDATE jobs SET job_summary = ? WHERE url = ?", (summary[:400], url))
                conn.commit()
                written += 1
        return written
    finally:
        conn.close()
