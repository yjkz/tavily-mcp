# ---- Stage 1: build the dashboard frontend -------------------------------
FROM node:22-slim AS frontend
WORKDIR /build
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# ---- Stage 2: runtime -----------------------------------------------------
FROM python:3.12-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Python dependencies first (cached unless the lockfile changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code + prebuilt frontend
COPY app/ ./app/
COPY --from=frontend /build/dist/ ./dashboard/dist/

# Non-root runtime user; /data is the SQLite volume mount point.
RUN useradd --system --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /srv
USER appuser

ENV DATA_DIR=/data \
    PATH="/srv/.venv/bin:$PATH"

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
