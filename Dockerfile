# syntax=docker/dockerfile:1.7
#
# Multi-target image for the AI Engineering Change Auditor.
#
#   docker build --target backend  -t eca-backend:dev .
#   docker build --target frontend -t eca-frontend:dev .
#
# or simply `docker compose up --build`, which builds both targets and a
# PostgreSQL database.  The backend image ships the agents/ directory (Flow
# Registry v1 YAML + validator) and examples/ (the clearly-labelled demo
# fixture) because both are runtime inputs, not build artefacts.

# ---------------------------------------------------------------------------
# backend
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# git is required: the auditor reads audited revisions through local git
# (worktrees, diffs, file-at-revision) and curl is used by the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY agents/ ./agents/
COPY examples/ ./examples/

# Runtime state (SQLite fallback DB, isolated execution workspaces).  Declared
# as a volume so nothing audited ever lands in an image layer.
RUN mkdir -p /srv/var && chown -R nobody:nogroup /srv/var
VOLUME ["/srv/var"]

ENV DUO_FLOW_CONFIG_PATH=/srv/agents/flows/change-audit/flow.yaml \
    DEMO_REPO_PATH=/srv/examples/discount-service \
    SANDBOX_ROOT=/srv/var/sandboxes

WORKDIR /srv/backend
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# Unprivileged by default; the linux-namespace isolation tier needs user
# namespaces at the *host* kernel level, not container privileges.  When the
# container cannot provide a stronger tier the backend falls back and reports
# the selected tier honestly on /api/health.
USER nobody

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------------------
# frontend
# ---------------------------------------------------------------------------
FROM node:20-alpine AS frontend

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /srv/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

EXPOSE 3000

# The Next.js server proxies /api/* to this URL (next.config.ts rewrites), so
# the browser never talks to the backend directly and never sees credentials.
ENV BACKEND_URL=http://backend:8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1

CMD ["npx", "next", "start", "-H", "0.0.0.0", "-p", "3000"]
