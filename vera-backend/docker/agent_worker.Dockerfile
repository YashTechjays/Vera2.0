# Build from the repo root:
#   docker build -f docker/agent_worker.Dockerfile -t vera-agent-worker .

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages packages
COPY apps/agent_worker apps/agent_worker
COPY apps/control_plane/pyproject.toml apps/control_plane/pyproject.toml
RUN mkdir -p apps/control_plane/src/control_plane && touch apps/control_plane/src/control_plane/__init__.py
RUN uv sync --frozen --no-dev --package agent-worker

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 10001 vera
WORKDIR /app
COPY --from=builder --chown=vera:vera /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER vera

# Bake the turn-detector ONNX model (+ languages.json/tokenizer) and Silero VAD
# assets into the image so jobs never download at runtime — LiveKit's recommended
# pattern for production workers. Routed through our entrypoint so download-files
# introspects exactly the plugins the worker registers (turn_detector, silero).
# Lands in the vera user's HF cache (/home/vera/.cache/huggingface); the runtime
# process is the same user, so the files are found. Needs huggingface.co reachable
# at build time. HF_HUB_OFFLINE at runtime then forbids any runtime fetch.
RUN python -m agent_worker.main download-files
ENV HF_HUB_OFFLINE=1

# LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET from Secret Manager via
# workload identity (GCP service principal) — never baked into the image.
CMD ["python", "-m", "agent_worker.main", "start"]
