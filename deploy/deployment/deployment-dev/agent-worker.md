# agent_worker

LiveKit voice agent worker — `vera-backend/apps/agent_worker`

## Requirements

- Docker (for containerised run)
- Or: Python 3.12, uv (for local run)
- A running LiveKit server
- Deepgram API key (speech-to-text)
- Cartesia API key (text-to-speech)
- Vertex AI / Gemini access (LLM)

## Build Docker Image

Build context must be the `vera-backend/` directory:

```bash
cd vera-backend
docker build -f docker/agent_worker.Dockerfile -t agent-worker:latest .
```

## Run

```bash
docker run \
  -e VERA_ENV=dev \
  -e LIVEKIT_URL=ws://host:7880 \
  -e LIVEKIT_API_KEY=your-livekit-key \
  -e LIVEKIT_API_SECRET=your-livekit-secret \
  -e DEEPGRAM_API_KEY=your-deepgram-key \
  -e CARTESIA_API_KEY=your-cartesia-key \
  -e GOOGLE_API_KEY=your-google-api-key \
  agent-worker:latest
```

The worker connects outbound to LiveKit — it exposes no ports.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `LIVEKIT_URL` | Yes | LiveKit server URL. `ws://` locally, `wss://` in production. |
| `LIVEKIT_API_KEY` | Yes | LiveKit API key. Must match the LiveKit server configuration. |
| `LIVEKIT_API_SECRET` | Yes | LiveKit API secret. Must match the LiveKit server configuration. |
| `DEEPGRAM_API_KEY` | Yes | Deepgram API key for speech-to-text (Deepgram Flux model). |
| `CARTESIA_API_KEY` | Yes | Cartesia API key for text-to-speech (Cartesia Sonic 3.5). |
| `GOOGLE_API_KEY` | Yes* | Google API key for Gemini via Vertex AI. |
| `GOOGLE_APPLICATION_CREDENTIALS` | Yes* | Path to a GCP service account JSON file. Alternative to `GOOGLE_API_KEY`. In GCP environments (GKE, Compute Engine), Application Default Credentials are provided automatically — neither variable is needed. |
| `VERA_ENV` | No | Environment name: `local`, `dev`, `staging`, `prod`. |
| `VERA_LANGFUSE_HOST` | No | Self-hosted Langfuse URL for tracing. Tracing is disabled if unset. |
| `VERA_LANGFUSE_PUBLIC_KEY` | No | Langfuse project public key. |
| `VERA_LANGFUSE_SECRET_KEY` | No | Langfuse project secret key. |

*One of `GOOGLE_API_KEY` or `GOOGLE_APPLICATION_CREDENTIALS` is required unless running on GCP with a service account attached.

## Ports

None — the worker connects outbound to the LiveKit server only. It does not listen on any port.

## Notes

- The worker registers itself with LiveKit as `vera-agent` on startup. It will not process calls until `control_plane` dispatches it to a room.
- Keep at least one instance running at all times so it is available to handle incoming calls immediately.
- The voice pipeline per call: **Deepgram (STT) → Gemini 2.5 Flash (LLM) → Cartesia (TTS)**
