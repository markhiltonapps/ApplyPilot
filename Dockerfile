# ApplyPilot server image: runs the discover → enrich → score → tailor → cover → pdf
# pipeline on a schedule and serves the results dashboard. Auto-apply (stage 6) is
# not included: it needs the Claude Code CLI and a real desktop Chrome session.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    APPLYPILOT_DIR=/data

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install . \
    && pip install --no-deps python-jobspy \
    && pip install pydantic tls-client requests markdownify regex curl_cffi \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY deploy/server.py ./deploy/server.py

VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "/app/deploy/server.py"]
