# syntax=docker/dockerfile:1
#
# FinAlly — one container, one port, one process (PLAN.md §3, §11).
#
# Stage 1 builds the Next.js static export with Node; stage 2 runs FastAPI with uv and
# serves that export as static files. Node does not survive into the runtime image.

# ---- stage 1: frontend -------------------------------------------------------
FROM node:20-slim AS frontend

WORKDIR /build

# Manifests first, so a source-only edit reuses the cached dependency layer.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
# `output: 'export'` in next.config.mjs — this writes /build/out.
RUN npm run build


# ---- stage 2: runtime --------------------------------------------------------
FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Same trick as above: lockfile-only sync first, so application edits do not reinstall the
# dependency tree. --no-dev drops pytest/httpx/rich, which the server never imports.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/ ./
RUN uv sync --frozen --no-dev

COPY --from=frontend /build/out ./static

# /app/db is where the named volume mounts. The schema lives in app/schema/ precisely so it
# is NOT shadowed by that mount (PLAN.md §4).
ENV FINALLY_DB_PATH=/app/db/finally.db \
    FINALLY_STATIC_DIR=/app/static \
    PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# `status: ok` means the schema is applied AND the market task is ticking — a readiness
# signal a compose `depends_on: service_healthy` gate can actually trust (Review.md D6).
# python, not curl: the slim image has no curl and the venv is already on PATH.
HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import json,urllib.request,sys; \
d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)); \
sys.exit(0 if d['status'] == 'ok' else 1)"

# EXACTLY ONE WORKER. Do not add --workers: the price cache and the market-data task live
# in process memory, so a second worker would serve different prices to different SSE
# clients and race the first on SQLite writes (PLAN.md §3, §11).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
