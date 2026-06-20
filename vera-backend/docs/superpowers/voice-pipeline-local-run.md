# Voice Pipeline — Local Run & Manual Validation Checklist

> **Synthetic / role-play data only.** The pipeline currently uses `PassthroughPHIBoundary`
> (no de-identification). Do NOT speak or type real patient, payer, or provider identifiers
> into any call session until the PHI codec is wired. See devops-todo row 8.

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Compose | for `just up` |
| `just` | task runner (see README) |
| Node 20+ / npm | for the frontend |
| Real vendor API keys | Deepgram, Cartesia, Google/Gemini (see §2) |

---

## 2. Required environment variables

**Do not commit a `.env` file with real keys.** Set the vars in your shell or an
uncommitted `.env` (gitignored) before running the services.

### Backend (repo root `.env` or shell)

| Variable | Local dev value | Notes |
|---|---|---|
| `VERA_LIVEKIT_URL` | `ws://localhost:7880` | URL the control plane uses to mint join tokens and dispatch the worker |
| `LIVEKIT_URL` | `ws://localhost:7880` | consumed by the agent worker via `EnvSecretProvider` |
| `LIVEKIT_API_KEY` | `devkey` | LiveKit `--dev` default |
| `LIVEKIT_API_SECRET` | `secret` | LiveKit `--dev` default |
| `DEEPGRAM_API_KEY` | *(your real key)* | STT; must be a Deepgram project key |
| `CARTESIA_API_KEY` | *(your real key)* | TTS; must be a Cartesia account key |
| `GOOGLE_API_KEY` **or** `GOOGLE_APPLICATION_CREDENTIALS` | *(your real cred)* | Gemini LLM via the `google` livekit-agents plugin; use a service-account JSON path or an API key depending on your project setup |
| `LOCAL_KMS_MASTER_KEY` | *(generate once — see CLAUDE.md)* | Required when `VERA_KMS_KEY_NAME` is not set |

Generate `LOCAL_KMS_MASTER_KEY` once:

```bash
python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

### Frontend (`vera-frontend/.env.local` or shell)

| Variable | Value | Notes |
|---|---|---|
| `VITE_API_URL` | `http://localhost:8000/api/v1` | Points the SPA at the local control plane |
| `VITE_DEV_TOKEN` | *(bearer token from `just seed` / `_mint` path)* | The SPA has no real auth flow yet; a dev token is a stopgap. Mint one via `just seed` (seeds demo data and prints a token) or the test `POST /api/v1/auth/_mint` endpoint if exposed locally |

---

## 3. Start all services

Run each command in a separate shell (or use a multiplexer like tmux).

### Backend repo (`vera-backend`)

```bash
# 1. Bring up Postgres, Redis, LiveKit, and sendria via docker-compose
just up
# Note: if port 1025/8025 (sendria) conflicts with a local mail service, stop
# that service first or edit docker-compose to remap the port.

# 2. Apply Alembic migrations
just migrate

# 3. Start the FastAPI control plane (port 8000)
just api

# 4. Start the LiveKit agent worker (registers as "vera-agent")
just worker
```

Expected signals:
- `just up` — all containers healthy; LiveKit logs `starting server` at `:7880`.
- `just migrate` — `Running upgrade … done` with no errors.
- `just api` — `Uvicorn running on http://0.0.0.0:8000`; `GET /healthz` returns 200.
- `just worker` — worker logs `registered worker` or `connected to livekit` as `vera-agent`.

### Frontend repo (`vera-frontend`)

```bash
# From the vera-frontend directory
npm run dev
# Vite starts at http://localhost:5173
```

---

## 3b. Quick cascade check — terminal console mode (no frontend)

To exercise the **Deepgram → Gemini → Cartesia** cascade with your laptop mic/speaker —
no LiveKit server, control plane, DB, or browser — run the worker in livekit-agents
`console` mode:

```bash
just worker-console
# equivalently: uv run python -m agent_worker.main console
```

Requirements: `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`, and Vertex ADC
(`gcloud auth application-default login`) in your env. `VERA_ENV` must be unset or
`local` (the default).

How it works: console mode connects the agent to a local room whose name isn't a vera
`call--…` room. `entrypoint` (`agent_worker/main.py`) normally rejects foreign rooms, but
`resolve_session` allows them **only when `settings.is_local`**, assigning a synthetic
session id (`"console"`). In any non-local environment a foreign room is still rejected, so
this path can never run an undispatched agent in dev/staging/prod. The PHI boundary is
`PassthroughPHIBoundary`, so the synthetic session is fine — **synthetic / role-play speech
only** (see the banner at the top).

You should hear the greeting, then converse turn-by-turn in the terminal. This validates the
voice loop in isolation; use §4 for the full call-dispatch + monitoring path.

## 4. Manual E2E walk (human validation)

> This section is a checklist for the human running the validation, not something
> Claude can automate. Tick each item as you go.

### 4.1 Trigger a call

- [ ] Open the frontend at `http://localhost:5173`.
- [ ] Navigate to **Data Management**.
- [ ] Open a patient record that is in status `READY FOR PROCESSING`.
- [ ] Set the status to **IN QUEUE**.
- [ ] Confirm in the worker log that a job is picked up.
- [ ] Confirm the control plane log shows `POST /api/v1/calls 200` (room created, worker dispatched).

> **Note on patient IDs:** Dummy/seed patient IDs may not be real form UUIDs wired to the
> backend call-dispatch logic. If `startCall` returns an error, you need a real form ID from
> the seeded data. Full backend data wiring (form-to-call linkage) is a later slice.

### 4.2 Live Monitoring

- [ ] Navigate to **Live Monitoring**.
- [ ] Confirm the new call appears in the list with the expected room name / status.
- [ ] Click **Intervene**.

### 4.3 InterveneModal — audio exchange

- [ ] The `InterveneModal` opens; the browser requests microphone permission — allow it.
- [ ] After a moment you should **hear the agent speak the greeting** (Cartesia TTS playing through the LiveKit room).
- [ ] Click **Speak** (or the microphone toggle) and speak as the payer representative.
- [ ] Observe:
  - [ ] The agent responds (STT → LLM → TTS round trip).
  - [ ] The **Live Transcripts** tab fills in with both turns.
- [ ] Switch between the **Info** and **Transcript** tabs — note that the transcript resets on tab-switch (known behaviour in this slice; tracked as a known open item).

---

## 5. Watch-items during validation

| Item | What to check |
|---|---|
| `useTranscriptions` is `@beta` | Verify the transcript renders correctly under the installed `@livekit/components-react` 2.9.21. If it renders blank, check the browser console for hook deprecation warnings. |
| Transcript resets on tab-switch | Switching Info ↔ Transcript in `InterveneModal` clears the transcript. This is a known issue in this slice; no action needed, just note it. |
| Turn latency | Target ~1.0–1.6 s end-to-end (US-hosted Deepgram + Cartesia + Vertex AI). If latency is significantly higher, check network routing to each vendor and whether the Gemini region is US. |
| livekit-agents deprecation warnings | The worker may log deprecation warnings about `endpointing` or `preemptive` (livekit-agents pipeline internals). These are **warnings only** — the pipeline still functions. No action needed in this slice. |
| PassthroughPHIBoundary | Confirm that only synthetic / role-play data is used. The passthrough seam is intentional for this slice; no real PHI must transit the pipeline. |

---

## 6. Teardown

```bash
# Stop the docker-compose stack (Postgres data persists in a named volume)
just down

# To wipe local state entirely (resets DB):
docker compose down -v
```
