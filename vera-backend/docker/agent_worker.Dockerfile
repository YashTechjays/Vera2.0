# Build from the repo root, ALWAYS pinning the platform CI builds:
#   docker build --platform linux/amd64 -f docker/agent_worker.Dockerfile -t vera-agent-worker .
#
# On Apple Silicon the native (linux/arm64) build FAILS at the download-files step below
# with `thread 'tokio-rt-worker' panicked ... panic in a function that cannot unwind` and
# exit 134. The assets download fine — livekit's Rust runtime aborts on teardown, after the
# work is done, and docker sees the non-zero exit. It arrived with livekit-agents 1.6.x
# (which adds the native `livekit-local-inference` wheel and moves rtc 1.1.8 -> 1.1.13);
# 1.5.17 builds clean on arm64. linux/amd64 is unaffected, so CI never sees it — which is
# exactly why it is easy to lose an afternoon to locally.

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
# Patch base-image OS packages to the latest security fixes (the image scan blocks on fixable CVEs).
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
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
