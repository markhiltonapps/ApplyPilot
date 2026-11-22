"""Free job sources that answer servers directly. No AI calls, no browser.

Three kinds:
  * remote job boards with public feeds: Remotive, Remote OK, Himalayas, Jobicy, We Work Remotely, Working Nomads
  * the public job boards of companies that hire through Greenhouse, Ashby or Lever (config/company_boards.yaml)
  * LinkedIn's public (signed-out) job search

Indeed is still searched through JobSpy. ZipRecruiter refuses cloud servers, and JobSpy's LinkedIn search is
rate-limited on every search from one, so LinkedIn is searched here instead, slowly.

The boards and company lists return every opening, so a job is kept only when its title is the kind of job
one of the job types in searches.yaml describes (see title_matcher). LinkedIn is already searched by job type.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import os
import random
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx
import yaml

from applypilot import config
from applypilot.config import APP_DIR, CONFIG_DIR
from applypilot.database import get_connection, init_db
from applypilot.discovery.jobspy import _location_ok
from applypilot.discovery.workday import strip_html

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request would bury the search log

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
STATE_PATH = APP_DIR / "feeds_state.json"
STRATEGY = "feed"
MATCH_VERSION = 3  # raise when title or place matching changes, so the boards are read again straight away


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# -- Matching a title to the job types being searched ------------------------

_STOP = {"of", "the", "and", "for", "in", "a", "an", "to", "at", "on", "with"}
_SAME = {"sr": "senior", "jr": "junior", "mgr": "manager", "dir": "director", "genai": "generative"}
_PHRASES = (("artificial intelligence", "ai"), ("vice president", "vp"), ("gen ai", "generative ai"),
            ("pre-sales", "presales"), ("pre sales", "presales"))


def _title_words(text: str) -> set[str]:
    text = (text or "").lower()
    for phrase, short in _PHRASES:
        text = text.replace(phrase, short)
    words = set()
    for w in re.findall(r"[a-z0-9+#]+", text):
        w = _SAME.get(w, w)
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]  # "Solutions Architect" and "Solution Architect" are the same job
        words.add(w)
    if "generative" in words:
        words.add("ai")
    return words


# Words that say how senior a job is, not what it is: ignored when comparing titles.
_LEVEL = {"senior", "junior", "principal", "staff", "founding", "associate", "i", "ii", "iii", "iv"}
# Words nearly every title has one of: two titles sharing only these are not the same kind of job.
_ROLE = {"manager", "lead", "engineer", "architect", "consultant", "strategist", "advisor", "director", "head",
         "specialist", "analyst", "vp", "officer", "executive", "leader", "designer", "developer", "builder"}


def company_key(name: str | None) -> str:
    """A company name reduced for comparing: "Retell AI", "retell.ai" and "Retell" are the same company."""
    key = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    key = re.sub(r"(inc|llc|ltd|corp|corporation|technologies|labs)$", "", key)
    return key[:-2] if key.endswith("ai") and len(key) > 4 else key


def title_matcher(queries: list[str]):
    """A function saying whether a job title is the kind of job one of the searched job types describes.

    Boards and company lists can't be searched the way Indeed can, so this stands in for the search. A title
    matches a job type when it shares two of its words, at least one of them saying what the job is about
    rather than just "manager" or "lead": "Voice AI Solutions Architect" matches "Solutions Architect" and
    "Solutions Engineer, Applied AI", but not "Engineering Manager". A job type of a single word needs that
    word. Seniority (senior, principal...) and filler words are ignored. Scoring then judges the real fit.
    """
    wanted = []
    for q in queries:
        words = frozenset(_title_words(q) - _STOP - _LEVEL)
        if words and words not in wanted:
            wanted.append(words)

    def match(title: str | None) -> bool:
        have = _title_words(title or "")
        for words in wanted:
            shared = words & have
            if len(shared) >= min(2, len(words)) and (shared - _ROLE or len(words) == 1):
                return True
        return False

    return match


# -- Where a remote job may be done from -------------------------------------

_HOME_COUNTRY = re.compile(
    r"\b(usa?|u\.s\.a?\.?|united states|americas?|north america|worldwide|anywhere|global|international)\b", re.I)
_US_STATE = re.compile(r",\s?(A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]"
                       r"|T[NX]|UT|V[AT]|W[AIVY])\b")
_ELSEWHERE = re.compile(
    r"\b(europe|european|emea|eu|apac|asia|latam|latin america|south america|africa|middle east|oceania|"
    r"uk|united kingdom|england|scotland|ireland|canada|mexico|brazil|argentina|colombia|chile|peru|"
    r"germany|france|spain|portugal|italy|netherlands|belgium|switzerland|austria|poland|romania|ukraine|"
    r"sweden|norway|denmark|finland|estonia|czech|czechia|hungary|greece|turkey|israel|uae|dubai|"
    r"saudi arabia|egypt|nigeria|kenya|south africa|india|pakistan|bangladesh|sri lanka|china|hong kong|"
    r"taiwan|japan|korea|singapore|malaysia|indonesia|thailand|vietnam|philippines|australia|new zealand|"
    r"lithuania|latvia|bulgaria|serbia|croatia|slovakia|slovenia|luxembourg|iceland|cyprus|malta|"
    r"armenia|kazakhstan|morocco|ghana|costa rica|uruguay|ecuador|venezuela|guatemala|panama|dominican republic|"
    r"london|berlin|paris|amsterdam|dublin|toronto|vancouver|bangalore|bengaluru|sydney|tokyo|munich|madrid|"
    r"barcelona|lisbon|warsaw|stockholm|zurich|tel aviv)\b", re.I)


def region_ok(text: str | None, country: str = "usa") -> bool:
    """Whether a job limited to `text` (a country, a region, a list of them) is open to someone in the US.

    Only clear cases are dropped ("Europe", "UK only", "Remote - India"); anything unclear is kept for scoring.
    Searches for another country are not narrowed.
    """
    if country not in ("usa", "us") or not text or not text.strip():
        return True
    if _HOME_COUNTRY.search(text) or _US_STATE.search(text):
        return True
    return not _ELSEWHERE.search(text)


# -- Small helpers -----------------------------------------------------------

def _client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=45, follow_redirects=True)


def _text(html: str | None) -> str:
    """Plain text from a description that may be HTML, or HTML that was escaped once more."""
    if not html:
        return ""
    if "&lt;" in html and "<" not in html:
        html = html_lib.unescape(html)
    return html_lib.unescape(strip_html(html))


def _when(value) -> datetime | None:
    """A timezone-aware datetime from the date forms the feeds use, or None."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            dt = parsedate_to_datetime(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _salary(low, high, currency: str = "", period: str = "") -> str | None:
    try:
        low, high = int(float(low or 0)), int(float(high or 0))
    except (ValueError, TypeError):
        return None
    if not low and not high:
        return None
    cur = "$" if (currency or "USD").upper() == "USD" else f"{currency} "
    text = f"{cur}{low:,}-{cur}{high:,}" if low and high and low != high else f"{cur}{(low or high):,}"
    return f"{text}/{period}" if period else text


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        log.warning("Could not save feed state: %s", e)


def _due(state: dict, key: str, hours: int) -> bool:
    """Whether a source last read more than `hours` ago should be read again."""
    last = _when(state.get(key))
    return last is None or datetime.now(timezone.utc) - last >= timedelta(hours=hours)


def _known(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone() is not None


def store(conn: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    """Save jobs, skipping ones already stored. Returns (new, already known)."""
    now = datetime.now(timezone.utc).isoformat()
    new = existing = 0
    for job in jobs:
        url = job.get("url")
        if not url or not job.get("title"):
            continue
        description = (job.get("description") or "").strip()
        full = description if len(description) > 200 else None
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, company, strategy, "
                "discovered_at, full_description, application_url, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job["title"].strip(), job.get("salary"), description[:500] or None, job.get("location"),
                 job["site"], job.get("company"), STRATEGY, now, full, job.get("apply_url"),
                 now if full else None),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1
    conn.commit()
    return new, existing


class _Filter:
    """What the person is searching for, read once from searches.yaml."""

    def __init__(self, cfg: dict):
        from applypilot.scoring.scorer import skip_terms, target_companies, title_skip_term

        self.targets = sorted({company_key(c) for c in target_companies()} - {""})
        self.queries = [q["query"] for q in cfg.get("queries", []) if q.get("query")]
        self.match = title_matcher(self.queries)
        self._skip_terms = skip_terms()
        self._title_skip_term = title_skip_term
        defaults = cfg.get("defaults", {}) or {}
        self.country = str(defaults.get("country_indeed", "usa")).lower()
        self.hours_old = int(defaults.get("hours_old", 72) or 72)
        self.cutoff = datetime.now(timezone.utc) - timedelta(hours=self.hours_old)
        self.locations = cfg.get("locations", []) or []
        self.want_remote = any(loc.get("remote") for loc in self.locations)
        self.accept = cfg.get("location_accept", []) or []
        self.reject = cfg.get("location_reject_non_remote", []) or []

    def skipped(self, title: str | None) -> bool:
        return bool(self._title_skip_term(title or "", self._skip_terms))

    def wanted(self, title: str | None, company: str | None = None) -> bool:
        """A title that fits a searched job type, or any title at a company the person especially wants
        (the quick title check and scoring then decide whether it suits them)."""
        if not title or self.skipped(title):
            return False
        return self.match(title) or (bool(self.targets) and company_key(company) in self.targets)

    def fresh(self, posted: datetime | None) -> bool:
        return posted is None or posted >= self.cutoff

    def place_ok(self, location: str | None, remote: bool) -> bool:
        """Remote jobs open to the person's country when they want remote work; otherwise jobs near them."""
        if remote or re.search(r"remote|anywhere|work from home|distributed", location or "", re.I):
            return self.want_remote and region_ok(location, self.country)
        return bool(location) and bool(self.accept) and _location_ok(location, self.accept, self.reject)


# -- Remote job boards -------------------------------------------------------

def _remotive(client: httpx.Client, f: _Filter) -> list[dict]:
    data = client.get("https://remotive.com/api/remote-jobs").json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "url": j.get("url"), "title": j.get("title"), "company": j.get("company_name"),
            "region": j.get("candidate_required_location"), "posted": _when(j.get("publication_date")),
            "description": _text(j.get("description")), "salary": (j.get("salary") or "").strip() or None,
        })
    return jobs


def _remoteok(client: httpx.Client, f: _Filter) -> list[dict]:
    data = client.get("https://remoteok.com/api").json()
    jobs = []
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue  # the first entry is the terms of use
        jobs.append({
            "url": j.get("url"), "title": j.get("position"), "company": j.get("company"),
            "region": j.get("location"), "posted": _when(j.get("date")),
            "description": _text(j.get("description")),
            "salary": _salary(j.get("salary_min"), j.get("salary_max"), "USD", "year"),
        })
    return jobs


def _himalayas(client: httpx.Client, f: _Filter) -> list[dict]:
    """Newest first, 20 a page. The board lists thousands a day, so read back a limited number of pages."""
    jobs = []
    for page in range(_env_int("HIMALAYAS_MAX_PAGES", 60)):
        r = client.get("https://himalayas.app/jobs/api", params={"limit": 20, "offset": page * 20})
        if r.status_code != 200:
            log.info("Himalayas: stopped at page %d (status %d)", page + 1, r.status_code)
            break
        batch = r.json().get("jobs", [])
        if not batch:
            break
        for j in batch:
            jobs.append({
                "url": j.get("guid") or j.get("applicationLink"), "title": j.get("title"),
                "company": j.get("companyName"), "region": ", ".join(j.get("locationRestrictions") or []),
                "posted": _when(j.get("pubDate")), "description": _text(j.get("description")),
                "salary": _salary(j.get("minSalary"), j.get("maxSalary"), j.get("currency") or "",
                                  {"annual": "year", "yearly": "year", "hourly": "hour",
                                   "monthly": "month"}.get(str(j.get("salaryPeriod") or "").lower(), "")),
            })
        last = _when(batch[-1].get("pubDate"))
        if last and last < f.cutoff:
            break
        time.sleep(0.4)
    return jobs


def _jobicy(client: httpx.Client, f: _Filter) -> list[dict]:
    geo = {"usa": "usa", "us": "usa", "uk": "uk", "canada": "canada"}.get(f.country)
    params = {"count": 100}
    if geo:
        params["geo"] = geo
    jobs = []
    for _ in range(_env_int("JOBICY_MAX_PAGES", 5)):
        r = client.get("https://jobicy.com/api/v2/remote-jobs", params=params)
        if r.status_code != 200:
            break
        data = r.json()
        batch = data.get("jobs", [])
        for j in batch:
            jobs.append({
                "url": j.get("url"), "title": html_lib.unescape(j.get("jobTitle") or ""),
                "company": html_lib.unescape(j.get("companyName") or ""), "region": j.get("jobGeo"),
                "posted": _when(j.get("pubDate")), "description": _text(j.get("jobDescription")),
                "salary": _salary(j.get("salaryMin"), j.get("salaryMax"), j.get("salaryCurrency") or "",
                                  {"yearly": "year", "hourly": "hour", "monthly": "month"}.get(
                                      str(j.get("salaryPeriod") or "").lower(), "")),
            })
        last = _when(batch[-1].get("pubDate")) if batch else None
        if not batch or not data.get("hasMore") or not data.get("nextCursor") or (last and last < f.cutoff):
            break
        params["cursor"] = data["nextCursor"]
        time.sleep(1)
    return jobs


def _weworkremotely(client: httpx.Client, f: _Filter) -> list[dict]:
    root = ET.fromstring(client.get("https://weworkremotely.com/remote-jobs.rss").content)
    jobs = []
    for item in root.iter("item"):
        get = {child.tag: (child.text or "").strip() for child in item}.get
        company, _, title = (get("title") or "").partition(": ")  # "Acme: Product Manager"
        if not title:
            company, title = "", get("title") or ""
        region = ", ".join(x for x in (get("region"), get("country")) if x)
        jobs.append({
            "url": get("link") or get("guid"), "title": title, "company": company or None, "region": region,
            "posted": _when(get("pubDate")), "description": _text(get("description")),
        })
    return jobs


def _workingnomads(client: httpx.Client, f: _Filter) -> list[dict]:
    jobs = []
    for j in client.get("https://www.workingnomads.com/api/exposed_jobs/").json():
        jobs.append({
            "url": j.get("url"), "title": j.get("title"), "company": j.get("company_name"),
            "region": j.get("location"), "posted": _when(j.get("pub_date")),
            "description": _text(j.get("description")),
        })
    return jobs


REMOTE_BOARDS = (
    ("Remotive", _remotive), ("Remote OK", _remoteok), ("Himalayas", _himalayas), ("Jobicy", _jobicy),
    ("We Work Remotely", _weworkremotely), ("Working Nomads", _workingnomads),
)


def search_remote_boards(conn: sqlite3.Connection, f: _Filter) -> dict:
    total_new = total_match = 0
    with _client() as client:
        for name, fetch in REMOTE_BOARDS:
            try:
                listed = fetch(client, f)
            except Exception as e:  # noqa: BLE001 - one board being down must not stop the others
                log.error("%s: could not be read: %s", name, e)
                continue
            keep = []
            for j in listed:
                if not (f.wanted(j.get("title"), j.get("company")) and f.fresh(j.get("posted"))
                        and region_ok(j.get("region"), f.country)):
                    continue
                region = (j.get("region") or "").strip()
                j["location"] = f"{region} (Remote)" if region else "Remote"
                j["site"] = name
                j["apply_url"] = j.get("url")
                keep.append(j)
            new, _ = store(conn, keep)
            total_new += new
            total_match += len(keep)
            log.info("%s: %d listed, %d match your job types, %d new", name, len(listed), len(keep), new)
    return {"matched": total_match, "new": total_new}


# -- Company job boards (Greenhouse, Ashby, Lever) ---------------------------

def load_company_boards() -> dict:
    path = CONFIG_DIR / "company_boards.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _greenhouse(client: httpx.Client, slug: str, name: str, f: _Filter, conn: sqlite3.Connection) -> tuple[int, list]:
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    listed = client.get(base).json().get("jobs", [])
    keep = []
    for j in listed:
        url = j.get("absolute_url")
        location = (j.get("location") or {}).get("name") or ""
        kind = " ".join(str(m.get("value") or "") for m in (j.get("metadata") or [])
                        if "location" in str(m.get("name") or "").lower() or "remote" in str(m.get("name") or "").lower())
        remote = bool(re.search(r"remote", kind, re.I))
        if not (url and f.wanted(j.get("title"), name) and f.place_ok(location, remote)
                and f.fresh(_when(j.get("first_published") or j.get("updated_at")))) or _known(conn, url):
            continue
        try:  # the list carries no description: fetch it for the few jobs that are kept
            detail = client.get(f"{base}/{j['id']}").json()
        except Exception as e:  # noqa: BLE001
            log.warning("%s: no description for %s: %s", name, j.get("title"), e)
            continue
        keep.append({"url": url, "title": j.get("title"), "company": name,
                     "location": f"{location} (Remote)" if remote and "remote" not in location.lower() else location,
                     "description": _text(detail.get("content")), "apply_url": url})
        time.sleep(0.2)
    return len(listed), keep


def _ashby(client: httpx.Client, slug: str, name: str, f: _Filter, conn: sqlite3.Connection) -> tuple[int, list]:
    data = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"}).json()
    listed = [j for j in data.get("jobs", []) if j.get("isListed", True)]
    keep = []
    for j in listed:
        url = j.get("jobUrl")
        places = [j.get("location") or ""] + [s.get("location") or "" for s in (j.get("secondaryLocations") or [])]
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or ""
        location = "; ".join(dict.fromkeys(p for p in places if p))
        # isRemote is also set on hybrid jobs; the workplace type says which it is when it's given.
        kind = str(j.get("workplaceType") or "").lower()
        remote = kind == "remote" or (not kind and bool(j.get("isRemote")))
        where = location if not country or country.lower() in location.lower() else f"{location}, {country}"
        if not (url and f.wanted(j.get("title"), name) and f.place_ok(where, remote) and f.fresh(_when(j.get("publishedAt")))):
            continue
        pay = ((j.get("compensation") or {}).get("scrapeableCompensationSalarySummary")
               or (j.get("compensation") or {}).get("compensationTierSummary"))
        keep.append({"url": url, "title": j.get("title"), "company": name,
                     "location": f"{location} (Remote)" if remote and "remote" not in location.lower() else location,
                     "description": j.get("descriptionPlain") or _text(j.get("descriptionHtml")),
                     "apply_url": j.get("applyUrl") or url, "salary": pay or None})
    return len(listed), keep


def _lever(client: httpx.Client, slug: str, name: str, f: _Filter, conn: sqlite3.Connection) -> tuple[int, list]:
    listed = client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}).json()
    keep = []
    for j in listed if isinstance(listed, list) else []:
        url = j.get("hostedUrl")
        cats = j.get("categories") or {}
        location = "; ".join(dict.fromkeys(cats.get("allLocations") or [cats.get("location") or ""]))
        remote = str(j.get("workplaceType") or "").lower() == "remote"
        where = f"{location}, {j['country']}" if j.get("country") and j["country"] != "US" else location
        if not (url and f.wanted(j.get("text"), name) and f.place_ok(where, remote) and f.fresh(_when(j.get("createdAt")))):
            continue
        parts = [j.get("descriptionPlain") or _text(j.get("description"))]
        parts += [f"{x.get('text', '')}\n{_text(x.get('content'))}" for x in (j.get("lists") or [])]
        parts.append(j.get("additionalPlain") or _text(j.get("additional")))
        keep.append({"url": url, "title": j.get("text"), "company": name,
                     "location": f"{location} (Remote)" if remote and "remote" not in location.lower() else location,
                     "description": "\n\n".join(p.strip() for p in parts if p and p.strip()),
                     "apply_url": j.get("applyUrl") or url})
    return len(listed) if isinstance(listed, list) else 0, keep


_SYSTEMS = (("greenhouse", _greenhouse), ("ashby", _ashby), ("lever", _lever))


def search_company_boards(conn: sqlite3.Connection, f: _Filter, boards: dict | None = None) -> dict:
    boards = load_company_boards() if boards is None else boards
    companies = listed_total = matched = total_new = failed = 0
    with _client() as client:
        for system, fetch in _SYSTEMS:
            for slug, name in (boards.get(system) or {}).items():
                companies += 1
                try:
                    listed, keep = fetch(client, str(slug), str(name), f, conn)
                except Exception as e:  # noqa: BLE001 - a company changing its board must not stop the rest
                    failed += 1
                    log.warning("%s: job board could not be read: %s", name, str(e)[:120])
                    continue
                for j in keep:
                    j["site"] = j.get("company") or name
                new, _ = store(conn, keep)
                listed_total += listed
                matched += len(keep)
                total_new += new
                if new:
                    log.info("%s: %d open jobs, %d match your job types, %d new", name, listed, len(keep), new)
    log.info("Company job boards: %d companies, %d open jobs, %d match your job types and location, %d new%s",
             companies, listed_total, matched, total_new, f" ({failed} could not be read)" if failed else "")
    return {"companies": companies, "matched": matched, "new": total_new, "failed": failed}


# -- LinkedIn ----------------------------------------------------------------

_LI = "https://www.linkedin.com"
_LI_COUNTRY = {"usa": "United States", "us": "United States", "uk": "United Kingdom", "canada": "Canada",
               "australia": "Australia", "india": "India", "germany": "Germany", "france": "France"}


class _Blocked(Exception):
    """LinkedIn keeps refusing: stop for this run."""


class _LinkedIn:
    """LinkedIn's public (signed-out) job search. It tolerates only a slow trickle of requests from one
    address, so requests are spaced out, a refusal is waited out once, and repeated refusals end the run."""

    def __init__(self, client: httpx.Client, deadline: float):
        self.client = client
        self.deadline = deadline
        self.pause = _env_int("LINKEDIN_PAUSE_SECONDS", 3)
        self.refusals = 0

    def get(self, url: str, params: dict | None = None) -> str | None:
        for attempt in (1, 2):
            if time.monotonic() > self.deadline:
                raise TimeoutError
            time.sleep(self.pause + random.uniform(0, 1.5))
            try:
                r = self.client.get(url, params=params)
            except httpx.HTTPError:
                return None
            if r.status_code == 200:
                self.refusals = 0
                return r.text
            if r.status_code not in (429, 403, 999):
                return None
            self.refusals += 1
            if self.refusals >= 5:
                raise _Blocked
            if attempt == 1:
                time.sleep(20)
        return None

    def search(self, keywords: str, location: str, hours_old: int, start: int = 0) -> list[dict]:
        from bs4 import BeautifulSoup

        params = {"keywords": keywords, "location": location, "f_TPR": f"r{hours_old * 3600}", "start": start}
        page = self.get(f"{_LI}/jobs-guest/jobs/api/seeMoreJobPostings/search", params)
        cards = []
        for card in BeautifulSoup(page or "", "html.parser").find_all(class_="base-search-card"):
            job_id = str(card.get("data-entity-urn", "")).split(":")[-1]
            title = card.find("h3")
            company = card.find("h4")
            place = card.find(class_="job-search-card__location")
            if job_id.isdigit() and title:
                cards.append({"id": job_id, "url": f"{_LI}/jobs/view/{job_id}",
                              "title": title.get_text(strip=True),
                              "company": company.get_text(strip=True) if company else None,
                              "place": place.get_text(strip=True) if place else ""})
        return cards

    def description(self, job_id: str) -> str:
        from bs4 import BeautifulSoup

        page = self.get(f"{_LI}/jobs-guest/jobs/api/jobPosting/{job_id}")
        body = BeautifulSoup(page or "", "html.parser").find(class_="show-more-less-html__markup")
        return _text(str(body)) if body else ""


_REMOTE_SAID = re.compile(
    r"\b(fully|100%|completely|entirely|primarily|permanently) remote\b"
    r"|\bremote[- ](first|based|position|role|opportunity|job|eligible|friendly|within|anywhere|us|usa|u\.s\.|united states)\b"
    r"|\b(us|usa|u\.s\.|united states|nationwide)[ ,:(/-]{1,3}remote\b"
    r"|\bwork(ing)? (remotely|from home|from anywhere)\b"
    r"|\b(this|the) (role|position|job|opportunity) is (a )?(fully )?remote\b"
    r"|\b(location|work ?place|work location|work arrangement|work model)( type)?\s*[:\-]\s*(fully )?remote\b"
    r"|\bremote\s*\((us|usa|u\.s\.|united states)", re.I)
_REMOTE_DENIED = re.compile(
    r"\b(not|isn't|is not) (a |an )?(remote|eligible for remote)\b|\bno remote\b|\bnon-remote\b"
    r"|\bremote work is not\b|\bnot (available|open) (for|to) remote\b", re.I)


def says_remote(title: str, place: str, description: str) -> bool:
    """Whether a listing clearly says the job is remote.

    LinkedIn's signed-out search ignores its own remote filter, so in a remote search most results are
    office jobs. Only listings that say remote in the title or location, or plainly in the text, are kept.
    """
    if re.search(r"\bremote\b", f"{title} {place}", re.I):
        return True
    return bool(_REMOTE_SAID.search(description or "")) and not _REMOTE_DENIED.search(description or "")


def search_linkedin(conn: sqlite3.Connection, f: _Filter) -> dict:
    """Search LinkedIn for each job type, a page at a time, and read the descriptions of jobs not seen before.

    Job types are taken in a random order and the search stops after LINKEDIN_MAX_MINUTES, so over several
    runs every job type gets its turn even though one run cannot cover them all. Listings already read and
    turned down (not remote) are remembered so they aren't read again.
    """
    minutes = _env_int("LINKEDIN_MAX_MINUTES", 25)
    if minutes <= 0 or not f.queries or not f.locations:
        return {"searched": 0, "new": 0}
    deadline = time.monotonic() + minutes * 60
    pages = max(1, _env_int("LINKEDIN_PAGES", 1))
    searches = [(q, loc) for q in f.queries for loc in f.locations]
    random.shuffle(searches)
    state = _load_state()
    passed_over = list(state.get("linkedin_passed_over", []))
    seen: set[str] = set(passed_over)
    searched = read = total_new = 0
    reason = "all job types searched"
    with _client() as client:
        li = _LinkedIn(client, deadline)
        try:
            for query, loc in searches:
                remote = bool(loc.get("remote"))
                if remote:  # ask for the word, since the filter is ignored
                    keywords, where = f"{query} remote", _LI_COUNTRY.get(f.country, f.country.title())
                else:
                    keywords, where = query, loc.get("location", "")
                keep = []
                try:
                    for page in range(pages):
                        cards = li.search(keywords, where, f.hours_old, start=page * 10)
                        fresh = [c for c in cards if c["id"] not in seen and not f.skipped(c["title"])]
                        seen.update(c["id"] for c in cards)
                        # Titles that match a job type first: descriptions are the slow part.
                        fresh.sort(key=lambda c: not f.match(c["title"]))
                        for c in fresh:
                            if _known(conn, c["url"]):
                                continue
                            place = c["place"]
                            if not remote and f.accept and not _location_ok(place, f.accept, f.reject):
                                passed_over.append(c["id"])
                                continue
                            text = li.description(c["id"])
                            if len(text) <= 200:
                                seen.discard(c["id"])  # no description this time: try again on a later run
                                continue
                            read += 1
                            if remote and not says_remote(c["title"], place, text):
                                passed_over.append(c["id"])
                                continue
                            keep.append({"url": c["url"], "title": c["title"], "company": c["company"],
                                         "location": (f"{place} (Remote)" if place else "Remote") if remote else place,
                                         "description": text, "site": "linkedin"})
                        if len(cards) < 10:
                            break
                finally:  # keep what was read even when the time limit or a refusal ends the search
                    total_new += store(conn, keep)[0]
                searched += 1
        except TimeoutError:
            reason = f"stopped at the {minutes}-minute limit"
        except _Blocked:
            reason = "LinkedIn stopped answering"
    state = _load_state()
    state["linkedin_passed_over"] = passed_over[-8000:]
    _save_state(state)
    log.info("LinkedIn: %d of %d searches done, %d listings read, %d new (%s)",
             searched, len(searches), read, total_new, reason)
    return {"searched": searched, "read": read, "new": total_new}


# -- Entry point -------------------------------------------------------------

def run_feeds_discovery(cfg: dict | None = None) -> dict:
    """Read the free sources and store jobs that match the searched job types."""
    cfg = config.load_search_config() if cfg is None else cfg
    if not cfg or not cfg.get("queries"):
        log.warning("No job types to search for yet.")
        return {"new": 0}
    init_db()
    conn = get_connection()
    f = _Filter(cfg)
    state = _load_state()
    stats: dict = {}
    every = _env_int("FEEDS_EVERY_HOURS", 6)
    # Changed job types, places or skip words: read everything again rather than wait.
    search_key = hashlib.sha256(json.dumps(
        [MATCH_VERSION, f.queries, f.locations, f.accept, f.hours_old, f.country, f._skip_terms, f.targets],
        sort_keys=True, default=str,
    ).encode()).hexdigest()[:16]
    if state.get("search_key") != search_key:
        state = {"search_key": search_key, "linkedin_passed_over": state.get("linkedin_passed_over", [])}

    # The boards and company lists change slowly and ask not to be read constantly.
    if f.want_remote and _due(state, "remote_boards", every):
        stats["remote_boards"] = search_remote_boards(conn, f)
        state["remote_boards"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
    if _due(state, "company_boards", every):
        stats["company_boards"] = search_company_boards(conn, f)
        state["company_boards"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
    stats["new"] = sum(s.get("new", 0) for s in stats.values() if isinstance(s, dict))
    if stats:
        log.info("Job boards and company job boards done: %d new jobs", stats["new"])
    else:
        log.info("Job boards and company job boards were read less than %d hours ago; not read again yet.", every)
    return stats


def run_linkedin_discovery(cfg: dict | None = None) -> dict:
    """Search LinkedIn (slow and rate-limited, so it runs after the quicker sources)."""
    cfg = config.load_search_config() if cfg is None else cfg
    if not cfg or not cfg.get("queries"):
        return {"new": 0}
    init_db()
    return search_linkedin(get_connection(), _Filter(cfg))
