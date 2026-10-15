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


def has_llm_key(data_dir: Path, environ: dict) -> bool:
    keys = ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL")
    return any(environ.get(k) for k in keys) or any(k in _env_values(data_dir) for k in keys)


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
        return "Google rejected that Gemini API key. Copy it again from aistudio.google.com/apikey."
    return None


def _lines(value: str) -> list[str]:
    return [s.strip() for s in re.split(r"[\n,]", value or "") if s.strip()]


def save(data_dir: Path, fields: dict[str, str], files: dict[str, tuple[str, bytes]], environ: dict) -> list[str]:
    """Validate the form and write ApplyPilot's config files. Returns error messages."""
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
    if resume_text is None and not (data_dir / "resume.txt").exists():
        errors.append("Add your resume: upload the file or paste the text.")

    # API key
    key = re.sub(r"\s", "", f.get("gemini_key", ""))
    if key:
        problem = _check_gemini_key(key)
        if problem:
            errors.append(problem)
    elif not has_llm_key(data_dir, environ):
        errors.append("Add your Gemini API key.")

    for name, label in (("full_name", "Full name"), ("email", "Email"), ("city", "City"),
                        ("country", "Country"), ("titles", "Job titles")):
        if not f.get(name):
            errors.append(f"{label} is required.")
    if f.get("titles") and not (f.get("want_remote") or f.get("want_local")):
        errors.append("Choose remote jobs, jobs near you, or both.")

    if errors:
        return errors

    # Everything validated: write files.
    if resume_text is not None:
        (data_dir / "resume.txt").write_text(resume_text + "\n", encoding="utf-8")
        if upload and upload[0].lower().endswith(".pdf"):
            (data_dir / "resume.pdf").write_bytes(upload[1])
    if key:
        env = _env_values(data_dir)
        env["GEMINI_API_KEY"] = key
        (data_dir / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")

    stored = {k: v for k, v in f.items() if k not in ("gemini_key", "resume_text")}
    (data_dir / "setup_form.json").write_text(json.dumps(stored, indent=2), encoding="utf-8")

    yes_no = {"yes": "Yes", "no": "No"}
    profile = {
        "personal": {
            "full_name": f["full_name"],
            "preferred_name": f.get("preferred_name", ""),
            "email": f["email"],
            "phone": f.get("phone", ""),
            "address": "",
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
            "target_role": _lines(f["titles"])[0],
        },
        "skills_boundary": {"skills": _lines(f.get("skills", ""))},
        "resume_facts": {
            "preserved_companies": _lines(f.get("companies", "")),
            "preserved_projects": [],
            "preserved_school": f.get("school", ""),
            "real_metrics": _lines(f.get("metrics", "")),
        },
        "eeo_voluntary": {
            "gender": "Decline to self-identify",
            "race_ethnicity": "Decline to self-identify",
            "veteran_status": "Decline to self-identify",
            "disability_status": "Decline to self-identify",
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
        "queries": [{"query": t, "tier": 1} for t in _lines(f["titles"])],
        "locations": locations,
        # Non-remote jobs are kept only when their location mentions one of these.
        "location_accept": [x for x in (f["city"], f.get("state", "")) if x] if f.get("want_local") else [],
        "location_reject_non_remote": [],
        "defaults": {
            "results_per_site": 50,
            "hours_old": 72,
            "country_indeed": INDEED_COUNTRIES.get(f["country"].lower(), f["country"].lower()),
        },
        "exclude_titles": EXCLUDE_TITLES,
    }
    (data_dir / "searches.yaml").write_text(
        "# Written by the ApplyPilot setup page. Saving the page again overwrites this file.\n"
        + yaml.safe_dump(searches, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

FORM_CSS = """
form.setup fieldset{border:1px solid var(--line);border-radius:10px;margin:0 0 18px;padding:14px}
form.setup legend{font-weight:600;padding:0 6px}
form.setup label{display:block;margin:10px 0 4px;font-size:15px}
form.setup input[type=text],form.setup input[type=email],form.setup input[type=password],
form.setup input[type=url],form.setup select,form.setup textarea{width:100%;box-sizing:border-box;
font:inherit;font-size:16px;padding:10px;border-radius:8px;border:1px solid var(--line);
background:var(--field);color:inherit}
form.setup textarea{min-height:90px}
form.setup .row{display:flex;gap:10px}form.setup .row>div{flex:1;min-width:0}
form.setup .check{display:flex;gap:8px;align-items:center;margin:8px 0}
form.setup .hint{color:var(--muted);font-size:14px;margin:4px 0 0}
form.setup button{width:100%;font-size:17px;padding:14px;border-radius:10px;border:0;
background:#2f6fed;color:#fff;font-weight:600}
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
        resume_note = f"✅ Resume saved ({words} words). Upload a new file only if you want to replace it."
    else:
        resume_note = "PDF, Word (.docx) or text file."
    key_saved = has_llm_key(data_dir, environ)
    key_hint = ("✅ Key saved. Leave blank to keep it." if key_saved else
                'Free. Open <a href="https://aistudio.google.com/apikey" target="_blank">aistudio.google.com/apikey</a>, '
                'sign in with Google, tap <b>Create API key</b>, copy it and paste it here.')

    top = ""
    if errors:
        top = '<div class="errors"><b>Please fix:</b><ul>' + "".join(
            f"<li>{html.escape(e)}</li>" for e in errors) + "</ul></div>"
    elif saved:
        top = ('<div class="ok"><b>Saved.</b> ApplyPilot is now searching for jobs. The first run can take '
               'up to an hour. Check progress on the <a href="/status">status page</a>.</div>')

    return f"""<p><a href="/status">← Status</a></p>
<h1>Set up ApplyPilot</h1>
<p class="hint">Fill this in once. You can come back and change it any time.</p>
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

<fieldset><legend>2. Gemini API key</legend>
<label for="gemini_key">API key{"" if key_saved else " *"}</label>
<input type="password" id="gemini_key" name="gemini_key" autocomplete="off" placeholder="{"••••••••" if key_saved else "AIza…"}">
<p class="hint">{key_hint}</p>
</fieldset>

<fieldset><legend>3. About you</legend>
{text("full_name", "Full name", req=True)}
{text("email", "Email", "email", req=True)}
{text("phone", "Phone")}
<div class="row"><div>{text("city", "City", req=True)}</div><div>{text("state", "State / province")}</div></div>
<div class="row"><div>{text("country", "Country", default="United States", req=True)}</div>
<div>{text("postal_code", "ZIP / postal code")}</div></div>
{text("linkedin_url", "LinkedIn URL", "url")}
{text("website_url", "Website or portfolio", "url")}
</fieldset>

<fieldset><legend>4. Jobs you want</legend>
{area("titles", "Job titles to search for", "One per line, e.g. Marketing Manager", req=True,
       placeholder="Product Marketing Manager")}
{check("want_remote", "Remote jobs", True)}
{check("want_local", "Jobs near my city", True)}
<div class="row"><div>{text("salary_min", "Salary minimum", hint="Yearly, numbers only")}</div>
<div>{text("salary_max", "Salary maximum")}</div></div>
{text("currency", "Currency", default="USD")}
{select("authorized", "Legally allowed to work in this country?", [("yes", "Yes"), ("no", "No")], "yes")}
{select("sponsorship", "Need visa sponsorship?", [("no", "No"), ("yes", "Yes")], "no")}
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

<button type="submit">Save and start searching</button>
</form>"""
