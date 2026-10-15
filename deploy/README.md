# Running ApplyPilot on a server

The `Dockerfile` at the repo root packages stages 1–5 of ApplyPilot (discover → enrich → score → tailor → cover letters → PDFs) together with `deploy/server.py`. The server:

- runs `applypilot run` when it starts, then every `RUN_INTERVAL_HOURS`
- serves the results dashboard, tailored resumes and cover letters over HTTP, behind basic auth

Stage 6 (auto-apply) isn't included. It needs the Claude Code CLI driving a real Chrome session, and job sites block headless cloud browsers. Run `applypilot apply` on your own machine using the materials this server produces.

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
| `RUN_ARGS` | no | Extra `applypilot run` arguments (default `--validation normal`), e.g. `--workers 2 --min-score 8` |
| `LLM_MODEL`, `PROXY`, `CAPSOLVER_API_KEY` | no | Same meaning as in the CLI |

The server rewrites the profile, resume and searches files from these variables on every start and before every run, so changing a variable updates the config.

## Endpoints

- `/` — the dashboard (redirects to `/setup` until configured)
- `/setup` — the setup form
- `/status` — run state, config problems, and a "Run pipeline now" button
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
