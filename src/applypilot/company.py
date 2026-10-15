"""Best available employer name for a job, for display.

Job boards (Indeed, LinkedIn...) are where a lead was found, not who is hiring. The company comes from the
listing when discovery recorded it; otherwise it's read off the application link where the employer's name is
part of it (greenhouse.io/<company>, <company>.wd5.myworkdayjobs.com, careers.<company>.com...).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

JOB_BOARDS = {"indeed", "linkedin", "glassdoor", "zip_recruiter", "ziprecruiter", "google", "bayt", "naukri",
              "bdjobs", "monster", "dice", "simplyhired", "careerbuilder", "jobspy"}

# Applicant-tracking systems that put the employer's slug in the path, and the hosts that carry it as a subdomain.
_PATH_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com", "workable.com", "jobvite.com",
               "rippling.com")
_SUBDOMAIN_HOSTS = ("myworkdayjobs.com", "bamboohr.com", "breezy.hr", "recruitee.com", "teamtailor.com",
                    "applytojob.com", "icims.com", "taleo.net", "pinpointhq.com")
# Boards, aggregators and ad/redirect networks: their name in a link says nothing about the employer.
_NOT_EMPLOYERS = ("indeed", "linkedin", "glassdoor", "ziprecruiter", "google", "monster", "dice", "simplyhired",
                  "appcast", "jooble", "adzuna", "talent.com", "lensa", "jobright", "careerbuilder", "snagajob",
                  "builtin", "wellfound", "otta", "jobot", "recruit.net", "joblist", "myjobhelper", "click.")
_GENERIC = {"www", "jobs", "job", "careers", "career", "apply", "boards", "job-boards", "join", "hire", "work",
            "en", "us", "app", "recruiting", "talent", "external", "embed", "api", "uk", "global"}


def is_job_board(site: str | None) -> bool:
    return (site or "").strip().lower().replace(" ", "_") in JOB_BOARDS


def _pretty(slug: str) -> str:
    slug = re.sub(r"[-_.]+", " ", slug).strip()
    if any(c.isupper() for c in slug):
        return slug
    return slug.upper() if len(slug) <= 3 else slug.title()


def from_url(url: str | None) -> str | None:
    """The employer named in an application link, if the link carries it."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if any(n in host for n in _NOT_EMPLOYERS):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    embed_for = re.search(r"(?:^|&)for=([^&]+)", parsed.query)
    if host.endswith("greenhouse.io") and embed_for:
        return _pretty(embed_for.group(1))
    if any(host.endswith(h) for h in _PATH_HOSTS) and parts:
        first = parts[0] if parts[0].lower() not in ("embed", "o", "j", "jobs") else (parts[1] if len(parts) > 1 else "")
        if first and not first.isdigit():
            return _pretty(first)
    labels = host.split(".")
    if any(host.endswith(h) for h in _SUBDOMAIN_HOSTS):
        sub = [label for label in labels[:-2] if label not in _GENERIC and not re.fullmatch(r"wd\d+", label)]
        return _pretty(sub[0]) if sub else None
    # The employer's own careers site: careers.acme.com, jobs.acme.com, acme.com/careers
    core = [label for label in labels[:-1] if label not in _GENERIC]
    if core:
        return _pretty(core[-1])
    return None


def company_for(company: str | None, site: str | None, application_url: str | None = None) -> str | None:
    """The hiring company's name, or None when it isn't known."""
    if company and company.strip() and company.strip().lower() != "nan":
        return company.strip()
    if site and not is_job_board(site):
        return site.strip()
    return from_url(application_url)


def board_label(site: str | None) -> str | None:
    """'Indeed', 'LinkedIn'... for jobs found on a board; None when the site is the employer itself."""
    if not is_job_board(site):
        return None
    names = {"linkedin": "LinkedIn", "zip_recruiter": "ZipRecruiter", "ziprecruiter": "ZipRecruiter"}
    key = (site or "").strip().lower().replace(" ", "_")
    return names.get(key, key.replace("_", " ").title())
