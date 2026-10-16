"""Browser setup form for the ApplyPilot server.

Lets someone configure ApplyPilot from a phone: upload a resume file, enter an
LLM API key, and fill in the profile and job-search details. Writes the same
files `applypilot init` would (resume.txt, resume.pdf, profile.json,
searches.yaml, .env) into the data directory.
"""

from __future__ import annotations

import html
import io
import json
import re
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path

import yaml

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Titles skipped in every search, copied from the packaged example config.
EXCLUDE_TITLES = [
    "senior director", "VP ", "vice president", "chief", "intern", "internship",
    "co-op", "clearance required", "TS/SCI",
]

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}

# python-jobspy's Indeed country names for the most common inputs.
INDEED_COUNTRIES = {
    "us": "usa", "usa": "usa", "united states": "usa", "united states of america": "usa",
    "uk": "uk", "united kingdom": "uk", "england": "uk", "canada": "canada",
    "australia": "australia", "ireland": "ireland", "india": "india", "germany": "germany",
}


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Split a multipart/form-data body into text fields and uploaded files."""
    msg = BytesParser(policy=policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            if payload:
                files[name] = (filename, payload)
        else:
            fields[name] = payload.decode("utf-8", errors="replace")
    return fields, files


# ---------------------------------------------------------------------------
# Resume text extraction
# ---------------------------------------------------------------------------

def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    paragraphs = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.DOTALL):
        para = re.sub(r"<w:tab/>", "\t", para)
        para = re.sub(r"<w:br/>", "\n", para)
        text = "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", para, flags=re.DOTALL))
        paragraphs.append(html.unescape(text))
    return "\n".join(paragraphs)


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_resume_text(filename: str, data: bytes) -> str:
    """Return plain text for an uploaded .pdf, .docx or .txt resume."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        text = _pdf_text(data)
    elif suffix == ".docx":
        text = _docx_text(data)
    elif suffix in (".txt", ".md", ""):
        text = data.decode("utf-8", errors="replace")
    else:
        raise ValueError(f"Unsupported file type '{suffix}'. Upload a PDF, Word (.docx) or text file.")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 200:
        raise ValueError("Couldn't read enough text from that file. If it's a scanned PDF, "
                         "upload a Word or text version, or paste the text instead.")
    return text


# ---------------------------------------------------------------------------
# Saved state
# ---------------------------------------------------------------------------

def _env_values(data_dir: Path) -> dict[str, str]:
    from dotenv import dotenv_values
    path = data_dir / ".env"
    return {k: v for k, v in dotenv_values(path).items() if v} if path.exists() else {}


OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "anthropic/claude-haiku-5.5"


def _write_env(data_dir: Path, env: dict[str, str]) -> None:
    (data_dir / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")


def current_provider(data_dir: Path) -> str:
    return "openrouter" if _env_values(data_dir).get("LLM_URL", "").startswith(OPENROUTER_URL) else "gemini"


def _provider_has_key(data_dir: Path, environ: dict, provider: str) -> bool:
    env = _env_values(data_dir)
    if provider == "openrouter":
        return bool(env.get("LLM_API_KEY"))
    return bool(environ.get("GEMINI_API_KEY") or env.get("GEMINI_API_KEY"))


def _check_openrouter_key(key: str) -> str | None:
    """Return an error message if OpenRouter rejects the key or the account has no credit."""
    import httpx
    headers = {"Authorization": f"Bearer {key}"}
    try:
        r = httpx.get(f"{OPENROUTER_URL}/key", headers=headers, timeout=15)
        if r.status_code in (401, 403):
            return "OpenRouter rejected that key. Copy it again from openrouter.ai/settings/keys."
        c = httpx.get(f"{OPENROUTER_URL}/credits", headers=headers, timeout=15)
        if c.status_code == 200:
            d = c.json().get("data", {})
            if float(d.get("total_credits") or 0) - float(d.get("total_usage") or 0) <= 0:
                return "That OpenRouter account has no credit. Add some at openrouter.ai/settings/credits."
    except (httpx.HTTPError, ValueError):
        return None
    return None


def has_llm_key(data_dir: Path, environ: dict) -> bool:
    keys = ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL")
    return any(environ.get(k) for k in keys) or any(k in _env_values(data_dir) for k in keys)


DECLINE = "Decline to self-identify"
EEO_OPTIONS = {
    "eeo_gender": ("Gender", ["Male", "Female", "Non-binary"]),
    "eeo_race": ("Race", ["White", "Black or African American", "Asian", "American Indian or Alaska Native",
                          "Native Hawaiian or Other Pacific Islander", "Two or more races"]),
    "eeo_hispanic": ("Hispanic or Latino?", ["No", "Yes"]),
    "eeo_veteran": ("Veteran status", ["I am not a protected veteran",
                                       "I identify as one or more of the classifications of protected veteran"]),
    "eeo_disability": ("Disability", ["No, I do not have a disability", "Yes, I have a disability"]),
}


def _saved_form(data_dir: Path) -> dict[str, str]:
    path = data_dir / "setup_form.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _check_gemini_key(key: str) -> str | None:
    """Return an error message if Google rejects the key, None if it works or can't be checked."""
    import httpx
    try:
        r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models",
                      params={"key": key, "pageSize": 1}, timeout=15)
    except httpx.HTTPError:
        return None
    if r.status_code in (400, 401, 403):
        print(f"[setup] Gemini key check failed: HTTP {r.status_code} {r.text[:300]}", flush=True)
        return "Google rejected that Gemini API key. Copy it again from aistudio.google.com/apikey."
    return None


def _lines(value: str) -> list[str]:
    return [s.strip() for s in re.split(r"[\n,]", value or "") if s.strip()]


def clean_titles(value: str) -> list[str]:
    """Job titles from the form, minus words the location checkboxes already cover."""
    titles: list[str] = []
    for line in _lines(value):
        t = re.sub(r"\b(remote|jobs?|hybrid|onsite|on-site)\b", " ", line, flags=re.IGNORECASE)
        t = re.sub(r"\s+", " ", t).strip(" -:;")
        if t and t.lower() not in (x.lower() for x in titles):
            titles.append(t)
    return titles


def _location_accept(f: dict[str, str]) -> list[str]:
    """Location strings that mark a non-remote job as local.

    Job boards already limit a city search to a radius, so this is kept
    broad: the city, the state, and the state's ", XX" abbreviation, which
    is how most listings write it (e.g. "Houston, TX").
    """
    state = f.get("state", "").strip()
    accept = [x for x in (f["city"].strip(), state) if x]
    abbrev = US_STATES.get(state.lower()) or (state.upper() if len(state) == 2 else "")
    if abbrev:
        accept.append(f", {abbrev}")
    return accept


def save(data_dir: Path, fields: dict[str, str], files: dict[str, tuple[str, bytes]], environ: dict) -> list[str]:
    """Validate the form and write ApplyPilot's config files. Returns error messages.

    The resume and API key are saved as soon as they're valid, even if other
    fields still have errors: browsers can't refill file and password inputs
    when the form is shown again, so holding them back would lose them.
    """
    errors: list[str] = []
    f = {k: v.strip() for k, v in fields.items()}
    data_dir.mkdir(parents=True, exist_ok=True)

    # Resume
    resume_text = None
    upload = files.get("resume_file")
    if upload:
        filename, data = upload
        if len(data) > MAX_UPLOAD_BYTES:
            errors.append("That resume file is over 10 MB.")
        else:
            try:
                resume_text = extract_resume_text(filename, data)
            except Exception as e:  # noqa: BLE001 - any parser failure is reported to the user
                errors.append(str(e) if isinstance(e, ValueError) else f"Couldn't read that file ({e}).")
    elif f.get("resume_text"):
        resume_text = fields["resume_text"].strip()
    if resume_text is not None:
        (data_dir / "resume.txt").write_text(resume_text + "\n", encoding="utf-8")
        if upload and upload[0].lower().endswith(".pdf"):
            (data_dir / "resume.pdf").write_bytes(upload[1])
    elif not (data_dir / "resume.txt").exists():
        errors.append("Add your resume: upload the file or paste the text.")

    # AI provider and key
    provider = f.get("provider") or current_provider(data_dir)
    if provider == "openrouter":
        key = re.sub(r"\s", "", f.get("openrouter_key", ""))
        problem = _check_openrouter_key(key) if key else None
        if problem:
            errors.append(problem)
        elif key or _provider_has_key(data_dir, environ, "openrouter"):
            env = _env_values(data_dir)
            env.update({"LLM_URL": OPENROUTER_URL, "LLM_MODEL": env.get("LLM_MODEL") or OPENROUTER_MODEL})
            if key:
                env["LLM_API_KEY"] = key
            _write_env(data_dir, env)
        else:
            errors.append("Add your OpenRouter API key.")
    else:
        key = re.sub(r"\s", "", f.get("gemini_key", ""))
        problem = _check_gemini_key(key) if key else None
        if problem:
            errors.append(problem)
        elif key or _provider_has_key(data_dir, environ, "gemini"):
            env = {k: v for k, v in _env_values(data_dir).items() if k not in ("LLM_URL", "LLM_MODEL")}
            if key:
                env["GEMINI_API_KEY"] = key
            _write_env(data_dir, env)
        else:
            errors.append("Add your Gemini API key.")

    for name, label in (("full_name", "Full name"), ("email", "Email"), ("city", "City"),
                        ("country", "Country"), ("titles", "Job titles")):
        if not f.get(name):
            errors.append(f"{label} is required.")
    if f.get("titles") and not clean_titles(f["titles"]):
        errors.append("Add at least one job title.")
    if f.get("titles") and not (f.get("want_remote") or f.get("want_local")):
        errors.append("Choose remote jobs, jobs near you, or both.")

    if errors:
        return errors

    stored = {k: v for k, v in f.items() if k not in ("gemini_key", "openrouter_key", "resume_text")}
    (data_dir / "setup_form.json").write_text(json.dumps(stored, indent=2), encoding="utf-8")
    write_config(data_dir, stored)
    return []


def saved_min_score(data_dir: Path) -> str:
    """Lowest fit score that gets a tailored resume, as chosen on the setup page (default 6)."""
    value = _saved_form(data_dir).get("min_score", "6")
    return value if value in ("5", "6", "7", "8") else "6"


def rebuild_from_saved(data_dir: Path) -> bool:
    """Regenerate profile.json and searches.yaml from the saved form, if there is one.

    Run at startup so improvements to how this file builds the config reach
    setups saved by an older version.
    """
    f = _saved_form(data_dir)
    if not all(f.get(k) for k in ("full_name", "email", "city", "country", "titles")):
        return False
    write_config(data_dir, f)
    return True


def write_config(data_dir: Path, f: dict[str, str]) -> None:
    """Write profile.json and searches.yaml from validated form values."""
    titles = clean_titles(f["titles"]) or _lines(f["titles"])
    yes_no = {"yes": "Yes", "no": "No"}
    profile = {
        "personal": {
            "full_name": f["full_name"],
            "preferred_name": f.get("preferred_name", ""),
            "email": f["email"],
            "phone": f.get("phone", ""),
            "address": f.get("address", ""),
            "city": f["city"],
            "province_state": f.get("state", ""),
            "country": f["country"],
            "postal_code": f.get("postal_code", ""),
            "linkedin_url": f.get("linkedin_url", ""),
            "github_url": "",
            "portfolio_url": f.get("website_url", ""),
            "website_url": f.get("website_url", ""),
            "password": "",
        },
        "work_authorization": {
            "legally_authorized_to_work": yes_no.get(f.get("authorized", "yes"), "Yes"),
            "require_sponsorship": yes_no.get(f.get("sponsorship", "no"), "No"),
            "work_permit_type": "",
            "citizenship": f.get("citizenship", ""),
        },
        "availability": {
            "earliest_start_date": f.get("start_date") or "Immediately",
            "available_for_full_time": "Yes",
            "available_for_contract": "No",
        },
        "compensation": {
            "salary_expectation": f.get("salary_max") or f.get("salary_min", ""),
            "salary_currency": f.get("currency") or "USD",
            "salary_range_min": f.get("salary_min", ""),
            "salary_range_max": f.get("salary_max", ""),
            "currency_conversion_note": "",
        },
        "experience": {
            "years_of_experience_total": f.get("years", ""),
            "education_level": f.get("education", ""),
            "current_job_title": f.get("current_title", ""),
            "current_company": "",
            "target_role": titles[0],
        },
        "skills_boundary": {"skills": _lines(f.get("skills", ""))},
        "resume_facts": {
            "preserved_companies": _lines(f.get("companies", "")),
            "preserved_projects": [],
            "preserved_school": f.get("school", ""),
            "real_metrics": _lines(f.get("metrics", "")),
        },
        "preferences": {"ai_only": bool(f.get("ai_only"))},
        "eeo_voluntary": {
            "gender": f.get("eeo_gender") or DECLINE,
            "race_ethnicity": f.get("eeo_race") or DECLINE,
            "hispanic_latino": f.get("eeo_hispanic") or DECLINE,
            "veteran_status": f.get("eeo_veteran") or DECLINE,
            "disability_status": f.get("eeo_disability") or DECLINE,
        },
    }
    (data_dir / "profile.json").write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")

    near = ", ".join(x for x in (f["city"], f.get("state", "")) if x)
    locations = []
    if f.get("want_local"):
        locations.append({"location": near, "remote": False})
    if f.get("want_remote"):
        locations.append({"location": "Remote", "remote": True})
    searches = {
        "queries": [{"query": t, "tier": 1} for t in titles],
        "locations": locations,
        # Non-remote jobs are kept only when their location mentions one of these.
        "location_accept": _location_accept(f) if f.get("want_local") else [],
        "location_reject_non_remote": [],
        "defaults": {
            "results_per_site": 50,
            "hours_old": 24 * int(f.get("days_back") or 7),
            "country_indeed": INDEED_COUNTRIES.get(f["country"].lower(), f["country"].lower()),
        },
        "exclude_titles": EXCLUDE_TITLES,
    }
    (data_dir / "searches.yaml").write_text(
        "# Written by the ApplyPilot setup page. Saving the page again overwrites this file.\n"
        + yaml.safe_dump(searches, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

FORM_CSS = """
form.setup fieldset{border:1px solid var(--line);border-radius:14px;margin:0 0 16px;padding:16px 18px 18px;background:var(--surface)}
form.setup legend{font-weight:650;padding:0 8px;background:var(--surface);border-radius:6px}
form.setup label{display:block;margin:10px 0 4px;font-size:15px}
form.setup input[type=text],form.setup input[type=email],form.setup input[type=password],
form.setup input[type=url],form.setup select,form.setup textarea{width:100%;box-sizing:border-box;
font:inherit;font-size:16px;padding:10px;border-radius:8px;border:1px solid var(--line);
background:var(--field);color:inherit}
form.setup textarea{min-height:90px}
form.setup .row{display:flex;gap:10px}form.setup .row>div{flex:1;min-width:0}
form.setup .check{display:flex;gap:8px;align-items:center;margin:8px 0}
form.setup label.choice{display:flex;gap:10px;align-items:flex-start;padding:12px;margin:8px 0;border:1px solid var(--line);
border-radius:10px;cursor:pointer;background:var(--bg)}
form.setup label.choice input{margin-top:4px}
form.setup label.choice:has(input:checked){border-color:var(--accent);background:#5b9dff14}
form.setup .hint{color:var(--muted);font-size:14px;margin:4px 0 0}
form.setup button{width:100%;font-size:17px;padding:14px;border-radius:10px;border:0;
background:var(--accent);color:var(--accent-ink);font-weight:650;cursor:pointer}
.errors{background:#4a1d1d;border:1px solid #a33;border-radius:10px;padding:10px 14px;margin:0 0 16px}
.ok{background:#173a22;border:1px solid #2f7d48;border-radius:10px;padding:10px 14px;margin:0 0 16px}
"""


def render(data_dir: Path, environ: dict, values: dict[str, str] | None = None,
           errors: list[str] | None = None, saved: bool = False) -> str:
    v = {**_saved_form(data_dir), **(values or {})}
    fresh = not v

    def val(name: str, default: str = "") -> str:
        return html.escape(v.get(name, default), quote=True)

    def text(name: str, label: str, kind: str = "text", hint: str = "", default: str = "", req: bool = False) -> str:
        star = " *" if req else ""
        h = f'<p class="hint">{hint}</p>' if hint else ""
        return (f'<label for="{name}">{label}{star}</label>'
                f'<input type="{kind}" id="{name}" name="{name}" value="{val(name, default)}"'
                f'{" required" if req else ""}>{h}')

    def area(name: str, label: str, hint: str = "", req: bool = False, placeholder: str = "") -> str:
        star = " *" if req else ""
        h = f'<p class="hint">{hint}</p>' if hint else ""
        return (f'<label for="{name}">{label}{star}</label><textarea id="{name}" name="{name}" '
                f'placeholder="{html.escape(placeholder)}"{" required" if req else ""}>{val(name)}</textarea>{h}')

    def select(name: str, label: str, options: list[tuple[str, str]], default: str) -> str:
        cur = v.get(name, default)
        opts = "".join(f'<option value="{k}"{" selected" if k == cur else ""}>{t}</option>' for k, t in options)
        return f'<label for="{name}">{label}</label><select id="{name}" name="{name}">{opts}</select>'

    def check(name: str, label: str, default_on: bool) -> str:
        on = v.get(name) if not fresh else default_on
        return (f'<div class="check"><input type="checkbox" id="{name}" name="{name}" value="1"'
                f'{" checked" if on else ""}><label for="{name}" style="margin:0">{label}</label></div>')

    resume_path = data_dir / "resume.txt"
    if resume_path.exists():
        words = len(resume_path.read_text(encoding="utf-8").split())
        resume_note = (f'✅ Resume saved ({words} words, <a href="/resume.txt" target="_blank">view it</a>). '
                       "Upload a new file only if you want to replace it.")
    else:
        resume_note = "PDF, Word (.docx) or text file."
    provider = v.get("provider") or current_provider(data_dir)
    gem_saved = _provider_has_key(data_dir, environ, "gemini")
    or_saved = _provider_has_key(data_dir, environ, "openrouter")
    gem_hint = ("✅ Key saved. Leave blank to keep it." if gem_saved else
                'Open <a href="https://aistudio.google.com/apikey" target="_blank">aistudio.google.com/apikey</a>, '
                'sign in with Google, tap <b>Create API key</b>, copy it and paste it here.')
    or_hint = ("✅ Key saved. Leave blank to keep it." if or_saved else
               '1. Sign up at <a href="https://openrouter.ai" target="_blank">openrouter.ai</a>. '
               '2. Add $5&ndash;10 at <a href="https://openrouter.ai/settings/credits" target="_blank">Credits</a>. '
               '3. Open <a href="https://openrouter.ai/settings/keys" target="_blank">Keys</a>, tap <b>Create</b>, '
               'copy the key (starts with sk-or-) and paste it here.')

    top = ""
    if errors:
        kept = [name for name, ok in (("resume", (data_dir / "resume.txt").exists()),
                                      ("API key", has_llm_key(data_dir, environ))) if ok]
        kept_note = (f"<p>Your {' and '.join(kept)} {'are' if len(kept) > 1 else 'is'} saved, "
                     "so you don't need to add them again.</p>") if kept else ""
        top = '<div class="errors"><b>Almost there. Please fix:</b><ul>' + "".join(
            f"<li>{html.escape(e)}</li>" for e in errors) + "</ul>" + kept_note + "</div>"
    elif saved:
        top = ('<div class="ok"><b>Saved.</b> ApplyPilot is now searching for jobs. The first run can take '
               'up to an hour. Check progress on the <a href="/status">status page</a>.</div>')

    return f"""<h1>Setup</h1>
<p class="sub">Fill this in once. You can come back and change it any time.</p>
{top}
<form class="setup" method="post" action="/setup" enctype="multipart/form-data">
<fieldset><legend>1. Your resume</legend>
<label for="resume_file">Upload your resume file</label>
<input type="file" id="resume_file" name="resume_file"
 accept=".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain">
<p class="hint">{resume_note}</p>
<details><summary class="hint">No file? Paste the text instead</summary>
<textarea name="resume_text" placeholder="Paste your whole resume here"></textarea></details>
</fieldset>

<fieldset><legend>2. AI service</legend>
<p class="hint" style="margin-top:0">ApplyPilot uses an AI service to score jobs and write your resumes.</p>
<label class="choice"><input type="radio" name="provider" value="openrouter"{" checked" if provider == "openrouter" else ""}>
<span><b>OpenRouter</b> &mdash; recommended. Pay as you go, about $1 or less per full search. Reliable.</span></label>
<label class="choice"><input type="radio" name="provider" value="gemini"{" checked" if provider == "gemini" else ""}>
<span><b>Google Gemini</b> &mdash; free, but often busy and slow.</span></label>
<div class="pk" data-for="openrouter">
<label for="openrouter_key">OpenRouter API key</label>
<input type="password" id="openrouter_key" name="openrouter_key" autocomplete="off" placeholder="{"••••••••" if or_saved else "sk-or-…"}">
<p class="hint">{or_hint}</p></div>
<div class="pk" data-for="gemini">
<label for="gemini_key">Gemini API key</label>
<input type="password" id="gemini_key" name="gemini_key" autocomplete="off" placeholder="{"••••••••" if gem_saved else "AIza…"}">
<p class="hint">{gem_hint}</p></div>
<script>
(function(){{var r=document.querySelectorAll('input[name=provider]');function u(){{var v=(document.querySelector('input[name=provider]:checked')||{{}}).value;
document.querySelectorAll('.pk').forEach(function(d){{d.style.display=d.dataset.for===v?'':'none'}})}}r.forEach(function(x){{x.addEventListener('change',u)}});u()}})();
</script>
</fieldset>

<fieldset><legend>3. About you</legend>
{text("full_name", "Full name", req=True)}
{text("email", "Email", "email", req=True)}
{text("phone", "Phone")}
{text("address", "Street address", hint="Optional. Some applications require it.")}
<div class="row"><div>{text("city", "City", req=True)}</div><div>{text("state", "State / province")}</div></div>
<div class="row"><div>{text("country", "Country", default="United States", req=True)}</div>
<div>{text("postal_code", "ZIP / postal code")}</div></div>
{text("linkedin_url", "LinkedIn URL", "url")}
{text("website_url", "Website or portfolio", "url")}
</fieldset>

<fieldset><legend>4. Jobs you want</legend>
{area("titles", "Job titles to search for",
       "One job title per line, e.g. AI Solutions Architect. Only titles: no headings, and no "
       "&ldquo;remote&rdquo; or city names (use the boxes below for that).", req=True,
       placeholder="AI Solutions Architect")}
{check("want_remote", "Remote jobs", True)}
{check("want_local", "Jobs near my city", True)}
{check("ai_only", "Only AI-focused roles (jobs where AI is a real part of the work, not just a mention)", False)}
<div class="row"><div>{select("days_back", "Look at jobs posted in the last",
    [("3", "3 days"), ("7", "week"), ("14", "2 weeks")], "7")}</div>
<div>{select("min_score", "Write resumes for jobs scoring",
    [("5", "5 or more (most)"), ("6", "6 or more"), ("7", "7 or more"), ("8", "8 or more (fewest)")], "6")}</div></div>
<p class="hint">Jobs are scored 1&ndash;10 for how well they fit you. A lower bar gives you more resumes to review.</p>
<div class="row"><div>{text("salary_min", "Salary minimum", hint="Yearly, numbers only")}</div>
<div>{text("salary_max", "Salary maximum")}</div></div>
{text("currency", "Currency", default="USD")}
{select("authorized", "Legally allowed to work in this country?", [("yes", "Yes"), ("no", "No")], "yes")}
{select("sponsorship", "Need visa sponsorship?", [("no", "No"), ("yes", "Yes")], "no")}
{select("citizenship", "Citizenship (some applications ask)", [("", "Choose one"), ("U.S. citizen", "U.S. citizen"),
    ("U.S. permanent resident (green card holder)", "U.S. permanent resident (green card)"),
    ("Not a U.S. citizen or permanent resident", "Neither")], "")}
{text("start_date", "Earliest start date", default="Immediately")}
</fieldset>

<fieldset><legend>5. Experience (helps the AI tailor your resume)</legend>
{text("current_title", "Current or most recent job title")}
<div class="row"><div>{text("years", "Years of experience")}</div>
<div>{text("education", "Highest education", hint="e.g. Bachelor's")}</div></div>
{text("school", "School name", hint="Kept exactly as written")}
{area("skills", "Your skills", "Comma-separated. The AI won't claim skills outside this list.")}
{area("companies", "Companies you've worked for", "Comma-separated. These always stay on your resume.")}
{area("metrics", "Real results to keep", "Comma-separated, e.g. grew revenue 40%, managed $2M budget")}
</fieldset>

<fieldset><legend>6. Voluntary questions</legend>
<p class="hint" style="margin-top:0">Many applications ask these optional equal-opportunity questions. Auto-apply
answers them with what you choose here; "{DECLINE}" means it picks the decline option.</p>
{"".join(select(name, label, [(o, o) for o in opts] + [(DECLINE, DECLINE)], DECLINE)
         for name, (label, opts) in EEO_OPTIONS.items())}
</fieldset>

<button type="submit">Save and start searching</button>
</form>"""
