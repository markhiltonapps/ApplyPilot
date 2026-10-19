"""Leave employers off every resume and cover letter (APPLYPILOT_DROP_EMPLOYERS, comma-separated names).

Removes their experience entries, the bullets and project entries that mention them, and sentences in other
text that do, from the master resume, the Setup companies list and resumes not yet sent. Idempotent: running
it again changes nothing once they're gone.
"""

from __future__ import annotations

import re

_YEAR = re.compile(r"\b(19|20)\d\d\b|\bpresent\b", re.I)


def names_from(value: str | None) -> list[str]:
    return [n.strip() for n in (value or "").split(",") if n.strip()]


def mentions(text: str, names: list[str]) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in names)


def _is_heading(line: str) -> bool:
    s = line.strip()
    return bool(s) and s == s.upper() and len(s) > 3 and any(c.isalpha() for c in s) and not s.startswith(("-", "•"))


def _is_bullet(line: str) -> bool:
    return line.strip().startswith(("- ", "• "))


def strip_text(text: str, names: list[str]) -> str:
    """The resume or letter without the given employers."""
    if not names or not mentions(text, names):
        return text
    out: list[str] = []
    section = ""
    skipping = False   # inside an entry being dropped
    seen_bullet = False
    for line in text.split("\n"):
        s = line.strip()
        if _is_heading(line):
            section, skipping = s, False
            out.append(line)
            continue
        if skipping:
            if not s:
                skipping = False
                continue
            if _is_bullet(line):
                seen_bullet = True
                continue
            if not seen_bullet and _YEAR.search(s):  # the entry's location/dates line
                continue
            skipping = False  # the next entry's header
        if not s:
            out.append(line)
            continue
        if mentions(s, names):
            if section in ("EXPERIENCE", "PROJECTS") and not _is_bullet(line):
                skipping, seen_bullet = True, False
                continue
            if _is_bullet(line):
                continue
            # A paragraph (summary, letter): keep the sentences that don't mention them.
            kept = [x for x in re.split(r"(?<=[.!?])\s+", s) if not mentions(x, names)]
            if kept:
                out.append(line[: len(line) - len(line.lstrip())] + " ".join(kept))
            continue
        out.append(line)
    cleaned = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def strip_list(value: str, names: list[str]) -> str:
    """A comma/line separated list (the Setup companies field) without the given employers."""
    parts = [p.strip() for p in re.split(r"[\n,]", value or "") if p.strip()]
    kept = [p for p in parts if not mentions(p, names) and not any(p.lower() in n.lower() for n in names)]
    return ", ".join(dict.fromkeys(kept))
