# Build from the repo root:
#   docker build -f docker/control_plane.Dockerfile -t vera-control-plane .

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages packages
COPY apps/control_plane apps/control_plane
COPY apps/agent_worker/pyproject.toml apps/agent_worker/pyproject.toml
RUN mkdir -p apps/agent_worker/src/agent_worker && touch apps/agent_worker/src/agent_worker/__init__.py
RUN uv sync --frozen --no-dev --package control-plane

COPY alembic.ini ./
COPY migrations migrations
COPY scripts scripts

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 10001 vera
WORKDIR /app
COPY --from=builder --chown=vera:vera /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER vera
EXPOSE 8000
# Migrations run as a release step / init container:
#   alembic upgrade head
CMD ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"]
