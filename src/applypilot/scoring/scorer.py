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


def _target_roles() -> list[str]:
    """The roles the candidate is looking for (from profile and searches), without repeats."""
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
    return list(dict.fromkeys(r.strip() for r in roles if r and r.strip()))


def _targets_line() -> str:
    """One prompt line naming the roles the candidate is looking for (from profile and searches)."""
    global _TARGETS
    if _TARGETS is None:
        roles = _target_roles()
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
        floor = min_salary()
        if floor:
            _TARGETS += (f"\n- The candidate's minimum base salary is ${floor:,} a year. If the posting states pay whose "
                         f"top end is below that (count hourly pay as x2080), score 3 or lower however well the rest "
                         "fits. If no pay is stated, ignore this.")
        if ai_only():
            _TARGETS += ("\n- The candidate ONLY wants roles where AI (machine learning, generative AI, LLMs, AI agents "
                         "or AI products) is central to the day-to-day work. If AI appears only in the company "
                         "description or is a minor part of the job, score 4 or lower however well the rest fits.")
    return _TARGETS


def min_salary() -> int:
    """The lowest yearly base salary the candidate accepts (Setup's salary minimum), or 0 when not set."""
    try:
        raw = str(load_profile().get("compensation", {}).get("salary_range_min") or "")
    except (FileNotFoundError, ValueError):
        return 0
    digits = re.sub(r"[^0-9]", "", raw.split(".")[0])
    value = int(digits) if digits else 0
    return value if value >= 1000 else 0


def ai_only() -> bool:
    """Whether the candidate asked (in Setup) for AI-focused roles only."""
    try:
        return bool(load_profile().get("preferences", {}).get("ai_only"))
    except (FileNotFoundError, ValueError):
        return False


# ── Filters that run before full scoring ──────────────────────────────────
# Full scoring sends the resume and the job description to the AI for every job. Company career
# sites return every opening they have, so most of that was spent on jobs nobody would apply to.

# Titles per quick-check call. Small enough that one bad reply only delays a few jobs.
TRIAGE_BATCH = 40

TRIAGE_PROMPT = """You screen job titles for a job seeker before a detailed review.
{targets}

Below is a numbered list of job titles, each with its company. Return the numbers of the titles that belong to a DIFFERENT JOB FAMILY from every role the candidate is looking for.

Judge by job family, not by exact wording, industry, product or seniority:
- Business roles are one broad family: sales, account management, business development, partnerships, marketing, product, program and project management, customer success, events, solutions and presales, consulting, strategy and general management. If any target role is a business role, KEEP every title in this family, whatever the industry or level.
- Drop only titles from another family altogether, when no target role is in that family. Examples: hands-on software, security, network or systems engineering and IT administration; clinical, lab or care work; skilled trades and driving; legal; accounting and bookkeeping; translation, language tutoring and data annotation; content creator gigs.

When a title could plausibly fit, or you are unsure, KEEP it. A detailed review follows, so wrongly keeping a title costs little and wrongly dropping one loses a good job.

Reply with only a JSON array of the numbers to drop, for example [2, 7]. Reply [] if none belong to a different job family."""


def skip_terms() -> list[str]:
    """Words or phrases from Setup: a job whose title contains one is skipped without using AI."""
    try:
        from applypilot.config import load_search_config
        terms = (load_search_config() or {}).get("exclude_titles") or []
    except Exception:  # noqa: BLE001 - no list means nothing is skipped
        return []
    return [str(t).strip() for t in terms if str(t).strip()]


def title_skip_term(title: str, terms: list[str]) -> str | None:
    """The first skip term that appears in the title as a whole word or phrase, if any.

    Whole words only, so "intern" skips "Marketing Intern" but not "International Sales Manager".
    """
    for term in terms:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", title or "", re.IGNORECASE):
            return term
    return None


def triage_titles(jobs: list[dict]) -> set[str]:
    """URLs of jobs whose title is clearly unrelated to the roles the candidate wants.

    One small AI call per batch of titles, in place of a full scoring call per job. Any problem
    (no target roles, the AI refusing, a reply that isn't a list) keeps the jobs for full scoring.
    """
    roles = _target_roles()
    if not roles or not jobs:
        return set()
    targets = "The candidate is looking for roles like: " + "; ".join(roles[:60]) + "."
    try:
        current = (load_profile().get("experience", {}).get("current_job_title") or "").strip()
    except (FileNotFoundError, ValueError):
        current = ""
    if current:
        targets += f" Their current or most recent title is: {current}."
    unrelated: set[str] = set()
    for start in range(0, len(jobs), TRIAGE_BATCH):
        batch = jobs[start:start + TRIAGE_BATCH]
        listing = "\n".join(
            f"{i + 1}. {(j.get('title') or '?')[:120]} "
            f"({company_for(j.get('company'), j.get('site'), j.get('application_url')) or j.get('site') or '?'})"
            for i, j in enumerate(batch))
        try:
            reply = get_client().chat(
                [{"role": "system", "content": TRIAGE_PROMPT.replace("{targets}", targets)},
                 {"role": "user", "content": listing}], max_tokens=256, temperature=0.0)
            found = re.search(r"\[[\d,\s]*\]", reply or "")
            numbers = json.loads(found.group(0)) if found else []
        except Exception as e:  # noqa: BLE001 - the quick check is an optimisation, never a blocker
            log.warning("Quick title check failed (%s); these %d jobs get a full score instead.", e, len(batch))
            continue
        picked = {n for n in numbers if isinstance(n, int) and 1 <= n <= len(batch)}
        unrelated.update(batch[n - 1]["url"] for n in picked)
        log.info("Quick title check: %d of %d titles are unrelated and skip full scoring.", len(picked), len(batch))
        if picked:
            log.info("Quick title check dropped: %s", "; ".join(
                (batch[n - 1].get("title") or "?")[:50] for n in sorted(picked)))
    return unrelated


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

    # Work through the jobs a group at a time, newest first, so scores start appearing straight
    # away even when thousands are waiting (a new resume re-scores everything). For each group,
    # first decide what can be scored without a full AI call: titles on the skip list (free), then
    # titles a quick check finds unrelated (one small call for the whole group).
    terms = skip_terms()
    skipped = 0
    for start in range(0, len(jobs), TRIAGE_BATCH):
        group = jobs[start:start + TRIAGE_BATCH]
        preset: dict[str, dict] = {}
        for job in group:
            term = title_skip_term(job.get("title") or "", terms)
            if term:
                preset[job["url"]] = {"score": 1, "keywords": "", "reasoning": (
                    f'Skipped without using AI: the title contains "{term}", which is on your skip list in Setup.')}
        for url in triage_titles([j for j in group if j["url"] not in preset]):
            preset[url] = {"score": 2, "keywords": "", "reasoning": (
                "Skipped after a quick title check: the title is unrelated to the roles you are looking for. "
                "The full description was not reviewed.")}
        skipped += len(preset)

        for job in group:
            result = dict(preset.get(job["url"]) or score_job(resume_text, job))
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
    if skipped:
        log.info("%d of %d jobs skipped full scoring.", skipped, len(jobs))

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
