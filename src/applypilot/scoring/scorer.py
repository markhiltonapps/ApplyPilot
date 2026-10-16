"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.company import company_for
from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

# Bump when the scoring instructions change, so the server re-scores jobs scored the old way.
SCORE_PROMPT_VERSION = 2

SCORE_PROMPT = """You are an experienced recruiter. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Judge the candidate against what THIS kind of role needs. For hands-on engineering roles, weigh
  programming languages, frameworks and tools. For leadership, solutions, consulting, presales,
  product, advocacy or customer-facing roles, weigh domain expertise, scope of responsibility,
  stakeholder and customer work, and comparable results instead.
- Count transferable experience that the resume actually shows.
- Be realistic about seniority: a role far below or far above the candidate's level is a weaker fit.
- The candidate wants full-time, salaried employment. Contract, 1099, freelance, temporary or part-time roles
  score 3 or lower however well they fit.
- {targets}

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning_lines: list[str] = []
    in_reasoning = False

    for raw in response.split("\n"):
        # Models sometimes bold the labels ("**SCORE:** 7") or vary their case.
        line = raw.strip().replace("**", "").strip()
        label = line.split(":", 1)[0].strip().upper() if ":" in line else ""
        value = line.split(":", 1)[1].strip() if ":" in line else line
        if label == "SCORE":
            in_reasoning = False
            try:
                score = int(re.search(r"\d+", value).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif label == "KEYWORDS":
            in_reasoning = False
            keywords = value
        elif label == "REASONING":
            in_reasoning = True
            reasoning_lines = [value] if value else []
        elif in_reasoning and line:
            reasoning_lines.append(line)

    reasoning = " ".join(reasoning_lines) or response.strip()
    return {"score": score, "keywords": keywords, "reasoning": reasoning}


_TARGETS: str | None = None


def _targets_line() -> str:
    """One prompt line naming the roles the candidate is looking for (from profile and searches)."""
    global _TARGETS
    if _TARGETS is None:
        roles: list[str] = []
        try:
            roles.append(load_profile().get("experience", {}).get("target_role", ""))
        except (FileNotFoundError, ValueError):
            pass
        try:
            from applypilot.config import load_search_config
            roles += [q.get("query", "") for q in (load_search_config() or {}).get("queries", [])]
        except Exception:  # noqa: BLE001 - targets are a hint; scoring works without them
            pass
        roles = list(dict.fromkeys(r.strip() for r in roles if r and r.strip()))
        _TARGETS = ("The candidate is looking for roles like: " + "; ".join(roles[:15])
                    + ". A job close to these and to their background should score higher than one "
                    "that only overlaps on skills." if roles else
                    "Consider whether the job matches the direction of the candidate's career.")
        try:
            personal = load_profile().get("personal", {})
        except (FileNotFoundError, ValueError):
            personal = {}
        home = ", ".join(x for x in (personal.get("city"), personal.get("province_state"),
                                     personal.get("country")) if x)
        if home:
            _TARGETS += (f"\n- The candidate lives in {home} and will not relocate unless the job says relocation is "
                         "paid. A remote job open only to residents of particular states or regions that don't "
                         "include theirs, or an onsite or hybrid job far from where they live, scores 3 or lower.")
        if ai_only():
            _TARGETS += ("\n- The candidate ONLY wants roles where AI (machine learning, generative AI, LLMs, AI agents "
                         "or AI products) is central to the day-to-day work. If AI appears only in the company "
                         "description or is a minor part of the job, score 4 or lower however well the rest fits.")
    return _TARGETS


def ai_only() -> bool:
    """Whether the candidate asked (in Setup) for AI-focused roles only."""
    try:
        return bool(load_profile().get("preferences", {}).get("ai_only"))
    except (FileNotFoundError, ValueError):
        return False


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {company_for(job.get('company'), job.get('site'), job.get('application_url')) or job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT.replace("{targets}", _targets_line())},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        return _parse_score_response(response)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    for job in jobs:
        result = score_job(resume_text, job)
        result["url"] = job["url"]
        completed += 1

        if result["score"] == 0:
            errors += 1

        results.append(result)
        # Save each score right away so progress shows up (and survives a restart).
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (result["score"], f"{result['keywords']}\n{result['reasoning']}",
             datetime.now(timezone.utc).isoformat(), result["url"]),
        )
        conn.commit()

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
