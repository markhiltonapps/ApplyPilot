# Running ApplyPilot on a server

The `Dockerfile` at the repo root packages stages 1–5 of ApplyPilot (discover → enrich → score → tailor → cover letters → PDFs) together with `deploy/server.py`. The server:

- runs `applypilot run` when it starts, then every `RUN_INTERVAL_HOURS`
- serves the results dashboard, tailored resumes and cover letters over HTTP, behind basic auth

Stage 6 (auto-apply) doesn't run on the server: it needs the Claude Code CLI driving a real Chrome session, and job sites block cloud browsers. Instead, the **companion** (`deploy/companion.py`, downloadable from the site's Auto-apply page with the server address filled in) runs it on your own PC:

1. installs the exact ApplyPilot version the server runs (`/api/package.tar.gz`),
2. downloads the jobs left to apply to, with their resumes and cover letters (`/api/bundle.zip`; duplicates of applied jobs are left out),
3. runs a practice pass (fills in, never submits) or real applications through `applypilot.apply.launcher`,
4. posts real outcomes to `/api/results`, which marks submitted jobs Applied and notes failures (CAPTCHA, login) on the job card.

Practice runs never mark anything applied. Jobs can also be marked applied by hand on the Ready to apply page.

**Website control.** Run the companion once and answer yes to "Let the website control auto-apply?": it adds Startup and Start-menu shortcuts and runs `companion.py --agent` in a minimized window from then on. The agent polls `/api/agent/poll` every few seconds; the Auto-apply page queues one run at a time (practice on a job, apply to one job, or apply to the next N), shows live progress from `/api/agent/status`, and has a Stop button (the agent kills the run's process tree, Chrome included). Results arrive at `/api/agent/done`, are recorded like `/api/results`, and are summarized on the page. Queue state lives in `/data/autoapply_control.json` (`deploy/agent_control.py`).

## Railway

`railway.json` configures the Dockerfile build and the `/health` healthcheck. Attach a volume at `/data` so the job database and generated files survive redeploys.

## Setup page

The easiest way to configure the server is the `/setup` page: upload a resume (PDF, Word or text), paste a Gemini API key, and fill in your details and the job titles to search for. It writes the same files as `applypilot init` to the data volume and starts a run. Until setup is complete, `/` redirects to `/setup`.

## Environment variables

These are optional if you use the setup page. When set, `APPLYPILOT_PROFILE_JSON`, `APPLYPILOT_RESUME_TXT` and `APPLYPILOT_SEARCHES_YAML` overwrite what the setup page saved.


| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | via setup page or here (or `OPENAI_API_KEY` / `LLM_URL`) | LLM for scoring, tailoring and cover letters |
| `APPLYPILOT_PROFILE_JSON` | via setup page or here | Full contents of your `profile.json` (see `profile.example.json`) |
| `APPLYPILOT_RESUME_TXT` | via setup page or here | Your resume as plain text |
| `APPLYPILOT_SEARCHES_YAML` | no | Contents of `searches.yaml`; the packaged example is used when unset |
| `DASHBOARD_PASSWORD` | yes, to view results | Basic-auth password; the web UI stays locked while it's unset |
| `DASHBOARD_USER` | no | Basic-auth user name (default `applypilot`) |
| `RUN_INTERVAL_HOURS` | no | Hours between runs (default `12`; `0` = only on start or on demand) |
| `RUN_ON_START` | no | Run immediately on boot (default `true`) |
| `RUN_ARGS` | no | Extra `applypilot run` arguments (default `--validation normal --stream --workers 2`, so scoring starts while discovery is still running), e.g. `--workers 2 --min-score 8` |
| `LLM_MODEL`, `PROXY`, `CAPSOLVER_API_KEY` | no | Same meaning as in the CLI |

The server rewrites the profile, resume and searches files from these variables on every start and before every run, so changing a variable updates the config.

## Endpoints

- `/` — the dashboard (redirects to `/setup` until configured)
- `/setup` — the setup form
- `/status` — run state, config problems, and a "Run pipeline now" button
- `/ready` — jobs with tailored documents: download, edit (`/edit`), Word export (`/docx`), mark applied (`/mark`)
- `/autoapply`, `/companion.py`, `/api/*` — the auto-apply companion and its endpoints
- `/files/tailored_resumes/`, `/files/cover_letters/` — generated documents
- `/health` — the unauthenticated healthcheck

## Local Docker

```bash
docker build -t applypilot-server .
docker run -p 8080:8080 -v applypilot-data:/data \
  -e GEMINI_API_KEY=... -e DASHBOARD_PASSWORD=... \
  -e APPLYPILOT_PROFILE_JSON="$(cat ~/.applypilot/profile.json)" \
  -e APPLYPILOT_RESUME_TXT="$(cat ~/.applypilot/resume.txt)" \
  applypilot-server
```

**Master resume builder** (`/onboard`, `deploy/onboarding.py`). New users upload their resume(s) and LinkedIn PDF;
the configured AI drafts one complete master resume (never inventing facts), asks up to 7 follow-up questions
about gaps (team sizes, results, missing dates), works the answers in, then suggests job titles with plain-English
descriptions and pre-fills Setup. Each AI step runs in a background thread; progress lives in
`/data/onboarding.json`.
