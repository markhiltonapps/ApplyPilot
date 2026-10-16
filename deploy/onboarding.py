"""Guided master-resume builder: upload what you have, answer a few questions, review, done.

The AI drafts one complete career history from the person's resumes and LinkedIn PDF, asks about the gaps
that matter most for matching (team sizes, numbers, results), works the answers in, and fills in Setup.
It never invents anything: missing details become questions instead.

Each AI step runs in a background thread; the page refreshes until it's done. State lives in
<data>/onboarding.json so a refresh or a server restart doesn't lose the person's progress.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import setup_form

log = logging.getLogger("onboarding")
_lock = threading.Lock()

MAX_SOURCE_CHARS = 60_000  # enough for a long resume plus a LinkedIn export

FORMAT_RULES = """FORMAT
- Plain text only: no tables, columns, markdown (#, **), or emoji. Bullets start with "- ".
- Line 1: full name. Line 2: a short professional headline. Line 3: City, State. Line 4: the email, phone and
  LinkedIn URL found in the sources, separated by " | ".
- Then these sections, headings in capitals, in this order: SUMMARY, SKILLS, EXPERIENCE, PROJECTS (only if
  there are any), EDUCATION, CERTIFICATIONS (only if there are any).
- EXPERIENCE: every job, including early ones, most recent first. Each title held is its own entry, even at
  the same company: promotions are separate entries, never merged. Entry header:
  Job Title, Company — City, State (Mon YYYY – Mon YYYY), or "Present" for a current role.
  4-8 bullets per role, each starting with a strong verb, keeping every real number (revenue, quota, %,
  team size, budget, users, units). Mention the tools and methods actually used.
- SKILLS: grouped lines such as "AI Platforms: OpenAI, Anthropic". Include every skill and tool the sources show.
- SUMMARY: 3-4 sentences covering the whole career, not aimed at any one job.
- Leave a section out entirely when there's nothing for it; never write "none" or placeholders.

RULES
- Use only facts found in the sources. Never invent numbers, titles, dates, employers, tools, degrees or
  certifications. Where sources disagree, prefer the more specific one. If a date or detail is missing, leave
  it out rather than guess."""

DRAFT_PROMPT = f"""You build a MASTER RESUME: a complete record of a person's career that a job-application
tool draws on to tailor a resume for each job. The tool never adds anything that isn't in the master resume,
so completeness matters more than length (3-5 pages is fine). The sources are the person's existing resumes,
LinkedIn profile export and notes.

{FORMAT_RULES}

Output only the resume text."""

REVISE_PROMPT = f"""You update a MASTER RESUME with the person's answers to follow-up questions. Work each
answer into the right places (the matching role's bullets, the summary, skills), written in resume language.
If an answer belongs in a section the resume doesn't have yet (for example EDUCATION, or a consulting role),
add that section or entry. Keep everything else as it is, word for word. Add nothing beyond what the answers
say; ignore answers that are blank. Every answer that isn't blank must appear somewhere in the result.

{FORMAT_RULES}

Output only the full updated resume text."""

EXTRACT_PROMPT = """You read a master resume and return JSON (and nothing else) with exactly these keys:

{
  "profile": {"full_name": "", "email": "", "phone": "", "city": "", "state": "", "country": "",
              "postal_code": "", "linkedin_url": "", "website_url": "", "current_title": "",
              "years": "", "education": "", "school": ""},
  "companies": ["every company the person worked FOR (employers and their own companies), exactly as written;
                 not customers, vendors, partners or schools"],
  "skills": ["every skill and tool listed or clearly used"],
  "metrics": ["up to 15 measurable results, each under 12 words, e.g. 'cut AI speech cost 70%'"],
  "titles": [{"title": "", "plain": ""}],
  "questions": [{"question": "", "why": ""}]
}

- profile: copy from the resume; leave a field "" if it isn't there. "years" is total years of work
  experience as a number. "education" is the highest degree, e.g. "BBA, Business Management". "country"
  defaults to "United States" when the location is a US city.
- titles: 8-12 job titles, as employers name them today, that this person is a strong fit for given their
  whole background, strongest first. Include newer titles (for example AI-related roles) when the resume
  supports them. "plain" is one short sentence in everyday words saying what that job does.
- questions: up to 7 short questions about information missing from the resume that would most improve
  job matches: team sizes managed and who was on them, numbers or results for roles that have none, scope
  (budgets, customers, products shipped), missing dates, consulting or advisory work. Ask only about things
  the resume doesn't already answer. Each must be answerable in a sentence or two. "why" says in a few
  words why it helps."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _path(data_dir: Path) -> Path:
    return data_dir / "onboarding.json"


def load(data_dir: Path) -> dict:
    try:
        return json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"stage": "upload"}


def _save(data_dir: Path, st: dict) -> None:
    st["updated"] = time.time()
    tmp = _path(data_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    tmp.replace(_path(data_dir))


def _update(data_dir: Path, **changes) -> dict:
    with _lock:
        st = load(data_dir)
        st.update(changes)
        _save(data_dir, st)
        return st


def restart(data_dir: Path) -> None:
    with _lock:
        _save(data_dir, {"stage": "upload"})


# ---------------------------------------------------------------------------
# AI calls
# ---------------------------------------------------------------------------

def _client(data_dir: Path):
    """An AI client for whichever provider Setup saved (the same one the pipeline uses)."""
    from applypilot.llm import LLMClient
    env = {**os.environ, **setup_form._env_values(data_dir)}
    if env.get("LLM_URL"):
        return LLMClient(env["LLM_URL"].rstrip("/"), env.get("LLM_MODEL") or setup_form.OPENROUTER_MODEL,
                         env.get("LLM_API_KEY", ""))
    if env.get("GEMINI_API_KEY"):
        return LLMClient("https://generativelanguage.googleapis.com/v1beta/openai",
                         env.get("LLM_MODEL") or "gemini-3.8-flash", env["GEMINI_API_KEY"])
    if env.get("OPENAI_API_KEY"):
        return LLMClient("https://api.openai.com/v1", env.get("LLM_MODEL") or "gpt-4o-mini", env["OPENAI_API_KEY"])
    raise RuntimeError("No AI service is set up yet. Add your AI key in Setup first.")


def _clean_resume(text: str) -> str:
    text = re.sub(r"^```[a-z]*\n|\n```\s*$", "", text.strip())
    return text.replace("**", "").strip()


def _parse_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The AI didn't return the expected details.")
    return json.loads(text[start:end + 1])


def _extract(client, resume: str) -> dict:
    reply = client.chat([{"role": "system", "content": EXTRACT_PROMPT},
                         {"role": "user", "content": f"MASTER RESUME:\n{resume}"}], max_tokens=4096)
    data = _parse_json(reply)
    profile = {k: str(v).strip() for k, v in (data.get("profile") or {}).items() if isinstance(v, str | int | float)}
    titles = [{"title": str(t.get("title", "")).strip(), "plain": str(t.get("plain", "")).strip()}
              for t in data.get("titles") or [] if isinstance(t, dict) and t.get("title")][:12]
    questions = [{"question": str(q.get("question", "")).strip(), "why": str(q.get("why", "")).strip()}
                 for q in data.get("questions") or [] if isinstance(q, dict) and q.get("question")][:7]
    as_list = lambda key: [str(x).strip() for x in data.get(key) or [] if str(x).strip()]
    # Only employers named in a job heading ("Title, Company — Place (dates)"), not vendors or customers.
    headings = [ln for ln in resume.splitlines() if " — " in ln and not ln.lstrip().startswith("-")]
    companies = [c for c in as_list("companies") if any(c.lower() in h.lower() for h in headings)]
    return {"profile": profile, "companies": companies, "skills": as_list("skills"),
            "metrics": as_list("metrics")[:15], "titles": titles, "questions": questions}


def _run(data_dir: Path, work) -> None:
    def target():
        try:
            work()
        except Exception as e:
            log.exception("Resume builder step failed")
            _update(data_dir, stage="error", error=str(e)[:300])
    threading.Thread(target=target, daemon=True).start()


def start_draft(data_dir: Path, sources: str) -> None:
    _update(data_dir, stage="drafting", error="", retry="draft", started=time.time())
    (data_dir / "onboarding_sources.txt").write_text(sources, encoding="utf-8")

    def work():
        client = _client(data_dir)
        draft = _clean_resume(client.chat([{"role": "system", "content": DRAFT_PROMPT},
                                           {"role": "user", "content": f"SOURCES:\n\n{sources}"}],
                                          max_tokens=12000))
        if len(draft.split()) < 80:
            raise ValueError("The draft came back too short. Try again, or add more detail to what you upload.")
        details = _extract(client, draft)
        stage = "questions" if details["questions"] else "review"
        _update(data_dir, stage=stage, draft=draft, **details)
    _run(data_dir, work)


def start_revise(data_dir: Path, answers: list[str]) -> None:
    st = _update(data_dir, stage="revising", error="", retry="revise", answers=answers, started=time.time())
    qa = [(q["question"], a.strip()) for q, a in zip(st.get("questions", []), answers, strict=False) if a.strip()]
    if not qa:
        _update(data_dir, stage="review")
        return

    def work():
        client = _client(data_dir)
        notes = "\n\n".join(f"Q: {q}\nA: {a}" for q, a in qa)
        revised = _clean_resume(client.chat(
            [{"role": "system", "content": REVISE_PROMPT},
             {"role": "user", "content": f"MASTER RESUME:\n{st['draft']}\n\n---\n\nANSWERS:\n{notes}"}],
            max_tokens=12000))
        if len(revised.split()) < len(st["draft"].split()) * 0.7:
            raise ValueError("The update came back shorter than the draft, so it wasn't used. Try again.")
        details = _extract(client, revised)
        details.pop("questions", None)
        _update(data_dir, stage="review", draft=revised, **details)
    _run(data_dir, work)


def retry(data_dir: Path) -> None:
    st = load(data_dir)
    if st.get("retry") == "revise" and st.get("draft"):
        start_revise(data_dir, st.get("answers", []))
        return
    src = data_dir / "onboarding_sources.txt"
    if src.exists():
        start_draft(data_dir, src.read_text(encoding="utf-8"))
    else:
        restart(data_dir)


def sources_from_upload(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> tuple[str, list[str]]:
    """Combine the uploaded files and pasted text into one source document. Returns (text, problems)."""
    parts, problems = [], []
    labels = {"resume1": "RESUME", "resume2": "ANOTHER RESUME", "linkedin": "LINKEDIN PROFILE"}
    for field, label in labels.items():
        upload = files.get(field)
        if not upload or not upload[1]:
            continue
        name, data = upload
        if len(data) > setup_form.MAX_UPLOAD_BYTES:
            problems.append(f"{name} is over 10 MB.")
            continue
        try:
            text = setup_form.extract_resume_text(name, data)
        except Exception as e:  # noqa: BLE001 - reported to the person
            problems.append(f"Couldn't read {name}: {e}")
            continue
        parts.append(f"=== {label} ({name}) ===\n{text.strip()}")
    if fields.get("notes", "").strip():
        parts.append(f"=== NOTES FROM THE PERSON ===\n{fields['notes'].strip()}")
    text = "\n\n".join(parts)[:MAX_SOURCE_CHARS]
    if not parts and not problems:
        problems.append("Upload at least one resume or your LinkedIn PDF, or paste your experience.")
    return text, problems


def finish(data_dir: Path, fields: dict[str, str]) -> list[str]:
    """Save the reviewed resume and fill Setup with the details. Returns problems, if any."""
    resume = fields.get("resume_text", "").replace("\r\n", "\n").strip()
    if len(resume.split()) < 50:
        return ["The resume is empty or too short."]
    (data_dir / "resume.txt").write_text(resume + "\n", encoding="utf-8")
    (data_dir / "resume.pdf").unlink(missing_ok=True)  # an older uploaded PDF no longer matches
    form = setup_form._saved_form(data_dir)
    for key in ("full_name", "email", "phone", "city", "state", "country", "postal_code", "linkedin_url",
                "website_url", "current_title", "years", "education", "school", "skills", "companies", "metrics"):
        value = fields.get(key, "").strip()
        if value or key not in form:
            form[key] = value
    titles = [t for t in fields.get("titles_picked", "").split("\n") if t.strip()]
    titles += [t.strip() for t in re.split(r"[\n,]", fields.get("titles_extra", "")) if t.strip()]
    if titles:
        form["titles"] = "\n".join(dict.fromkeys(titles))
    for box in ("want_remote", "want_local"):  # Setup stores ticked boxes as "1" and leaves unticked ones out
        if fields.get(box):
            form[box] = "1"
        else:
            form.pop(box, None)
    form = {k: v for k, v in form.items() if v != ""}
    (data_dir / "setup_form.json").write_text(json.dumps(form, indent=2), encoding="utf-8")
    _update(data_dir, stage="done")
    return []


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

CSS = """<style>
.ob-steps{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px;padding:0;list-style:none;font-size:13.5px}
.ob-steps li{padding:5px 11px;border-radius:999px;background:var(--surface);border:1px solid var(--line);color:var(--muted)}
.ob-steps li.on{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
.ob-steps li.done{color:var(--good);border-color:#22c55e55}
.ob label{display:block;font-weight:600;margin:14px 0 6px}
.ob input[type=text],.ob input[type=email],.ob textarea{width:100%;padding:10px 12px;border-radius:10px;
  border:1px solid var(--line);background:var(--field);color:var(--text);font:inherit}
.ob input[type=file]{width:100%;padding:10px;border:1px dashed var(--line);border-radius:10px;background:var(--field);color:var(--muted)}
.ob textarea{min-height:90px;resize:vertical}
.ob textarea.resume{min-height:520px;font:13.5px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}
.ob .hint{color:var(--muted);font-size:13.5px;margin:6px 0 0}
.ob .q{padding:14px 0;border-top:1px solid var(--line)}.ob .q:first-child{border-top:0}
.ob .q b{display:block;margin-bottom:4px}
.ob .grid{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
@media (max-width:600px){.ob .grid{grid-template-columns:1fr}}
.ob .title{display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-top:1px solid var(--line);font-weight:400}
.ob .title:first-child{border-top:0}
.ob .title input{margin-top:5px}.ob .title span{color:var(--muted);font-size:14px}
.ob .title b{color:var(--text)}
.ob .actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
.ob pre{white-space:pre-wrap;font:13px/1.5 ui-monospace,Consolas,monospace;color:var(--muted);max-height:340px;overflow:auto}
.ob .err{background:#ef44441a;border:1px solid #ef444455;border-radius:10px;padding:10px 14px;color:#fca5a5}
</style>"""

STEPS = (("upload", "1. Upload"), ("questions", "2. A few questions"), ("review", "3. Review"))


def _steps(stage: str) -> str:
    order = {"upload": 0, "drafting": 0, "questions": 1, "revising": 1, "review": 2, "done": 3, "error": -1}
    cur = order.get(stage, 0)
    items = []
    for i, (_, label) in enumerate(STEPS):
        cls = "on" if i == cur else ("done" if i < cur else "")
        items.append(f'<li class="{cls}">{"✓ " if i < cur else ""}{label}</li>')
    return f'<ol class="ob-steps">{"".join(items)}</ol>'


def render(data_dir: Path, environ: dict, problems: list[str] | None = None) -> tuple[str, bool]:
    """The page body for the current step, and whether it should refresh itself."""
    st = load(data_dir)
    stage = st.get("stage", "upload")
    if stage in ("drafting", "revising") and time.time() - st.get("started", 0) > 600:
        # The server restarted mid-step (or the AI hung): let the person try again.
        st = _update(data_dir, stage="error", error="That took too long, probably because the server restarted.")
        stage = "error"
    esc = html.escape
    err = ""
    if problems:
        err = '<div class="err">' + "<br>".join(esc(p) for p in problems) + "</div>"
    head = ('<h1>Build your master resume</h1><p class="sub">About 15 minutes. Upload what you have, answer a few '
            "questions, and ApplyPilot writes one complete resume it can tailor for every job, then fills in "
            "Setup for you.</p>")

    if stage in ("drafting", "revising"):
        what = ("Reading everything you uploaded and writing your master resume" if stage == "drafting"
                else "Adding your answers to your resume")
        return (f"""{head}{_steps(stage)}<div class="card status"><span class="dot run"></span><div>
<div class="head">{what}...</div><p class="muted">This takes about a minute. This page updates by itself.</p>
</div></div>""", True)

    if stage == "error":
        return (f"""{head}<div class="card ob"><div class="err"><b>That step didn't work:</b> {esc(st.get("error", ""))}</div>
<div class="actions"><form method="post" action="/onboard/retry"><button class="btn primary">Try again</button></form>
<form method="post" action="/onboard/restart"><button class="btn ghost">Start over</button></form></div></div>""", False)

    if stage == "questions":
        qs = "".join(f"""<div class="q"><b>{i + 1}. {esc(q["question"])}</b>
<span class="hint">{esc(q["why"])}</span><textarea name="a{i}" placeholder="Leave blank to skip"></textarea></div>"""
                     for i, q in enumerate(st.get("questions", [])))
        return (f"""{head}{_steps(stage)}<div class="card ob">
<h2>A few questions to make your resume stronger</h2>
<p class="hint" style="margin-top:-6px">These fill gaps that make a big difference to which jobs you match.
A sentence or two each is plenty. Skip any you don't want to answer.</p>
<form method="post" action="/onboard/answers">{qs}
<div class="actions"><button class="btn primary" name="go" value="answer">Add my answers</button>
<button class="btn ghost" name="go" value="skip">Skip these</button></div></form>
<details style="margin-top:18px"><summary class="hint">See the draft so far</summary><pre>{esc(st.get("draft", ""))}</pre></details>
</div>""", False)

    if stage == "review":
        p = st.get("profile", {})
        form = setup_form._saved_form(data_dir)

        def field(name: str, label: str) -> str:
            value = p.get(name) or form.get(name, "")
            return (f'<div><label for="{name}">{label}</label><input type="text" id="{name}" name="{name}" '
                    f'value="{esc(value, quote=True)}"></div>')
        titles = "".join(
            f'<label class="title"><input type="checkbox" name="title_pick" value="{esc(t["title"], quote=True)}"'
            f'{" checked" if i < 6 else ""}><div><b>{esc(t["title"])}</b><br><span>{esc(t["plain"])}</span></div></label>'
            for i, t in enumerate(st.get("titles", [])))
        remote_on = form.get("want_remote", "1") if form else "1"
        local_on = form.get("want_local", "1") if form else "1"
        return (f"""{head}{_steps(stage)}{err}<form method="post" action="/onboard/finish" class="ob">
<div class="card"><h2>Your master resume</h2>
<p class="hint" style="margin-top:-6px">Read it through and fix anything that's wrong or missing. Nobody else
sees this version; ApplyPilot uses it to write a tailored resume for each job.</p>
<textarea class="resume" name="resume_text">{esc(st.get("draft", ""))}</textarea></div>
<div class="card"><h2>Jobs to search for</h2>
<p class="hint" style="margin-top:-6px">Based on your whole background. Tick the ones that sound like you.</p>
{titles}
<label for="titles_extra">Other job titles (one per line)</label><textarea id="titles_extra" name="titles_extra"></textarea>
<label class="title"><input type="checkbox" name="want_remote" value="1"{" checked" if remote_on else ""}><div><b>Remote jobs</b></div></label>
<label class="title"><input type="checkbox" name="want_local" value="1"{" checked" if local_on else ""}><div><b>Jobs near my city</b></div></label>
</div>
<div class="card"><h2>Your details</h2><p class="hint" style="margin-top:-6px">Pulled from your resume. These
go on applications, so check them.</p>
<div class="grid">{field("full_name", "Full name")}{field("email", "Email")}{field("phone", "Phone")}
{field("linkedin_url", "LinkedIn URL")}{field("city", "City")}{field("state", "State")}
{field("country", "Country")}{field("postal_code", "ZIP code")}{field("current_title", "Current or most recent title")}
{field("years", "Years of experience")}{field("education", "Highest education")}{field("school", "School")}</div>
<label for="companies">Companies you've worked for</label><textarea id="companies" name="companies">{esc(", ".join(st.get("companies", [])))}</textarea>
<label for="skills">Skills</label><textarea id="skills" name="skills">{esc(", ".join(st.get("skills", [])))}</textarea>
<p class="hint">The AI only claims skills in this list when it tailors your resume.</p>
<label for="metrics">Results to keep on every resume</label><textarea id="metrics" name="metrics">{esc(", ".join(st.get("metrics", [])))}</textarea>
</div>
<div class="actions"><button class="btn primary" type="submit">Use this resume</button>
<button class="btn ghost" type="submit" formaction="/onboard/restart" formnovalidate>Start over</button></div>
<p class="hint">Next you'll see Setup with all of this filled in. Add your salary range and voluntary questions,
then click Save.</p></form>""", False)

    # upload (and "done", which offers a fresh start)
    if not setup_form.has_llm_key(data_dir, environ):
        return (f"""{head}{_steps("upload")}<div class="card"><h2>First, add your AI key</h2>
<p class="muted">The resume builder uses the same AI service as the rest of ApplyPilot. Open
<a href="/setup">Setup</a>, fill in <b>2. AI service</b> (just that part is enough) and click Save, then
come back here.</p></div>""", False)
    done = ('<div class="card ok" style="margin-bottom:14px">You already built a master resume. Uploading again '
            "starts a new one.</div>") if stage == "done" else ""
    return (f"""{head}{_steps("upload")}{done}{err}<form class="card ob" method="post" action="/onboard/start"
 enctype="multipart/form-data">
<h2>Upload what you have</h2>
<p class="hint" style="margin-top:-6px">Any resume works, even one written for a single job. More sources means a
more complete result.</p>
<label for="resume1">Your resume</label><input type="file" id="resume1" name="resume1" accept=".pdf,.docx,.txt">
<label for="linkedin">Your LinkedIn profile (PDF)</label><input type="file" id="linkedin" name="linkedin" accept=".pdf">
<p class="hint">On LinkedIn, click <b>Me</b> → <b>View profile</b> → <b>Resources</b> (or <b>More</b>) →
<b>Save to PDF</b>.</p>
<label for="resume2">Another resume (optional)</label><input type="file" id="resume2" name="resume2" accept=".pdf,.docx,.txt">
<label for="notes">Anything else (optional)</label>
<textarea id="notes" name="notes" placeholder="Jobs or projects missing from your resume, team sizes, results you're proud of..."></textarea>
<div class="actions"><button class="btn primary" type="submit">Build my master resume</button></div>
<p class="hint">Uses your AI service for a couple of cents. Nothing here is sent anywhere else.</p>
</form>""", False)
