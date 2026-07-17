# Resilient LLM utility + on-demand live-call summary — design

**Date:** 2026-07-17
**Status:** approved for planning
**Scope:** backend only (vera_core utility + control-plane endpoint). The frontend
tabbed Live Monitoring view (Transcription | Summary) consumes the endpoint and is a
follow-up slice.

## Problem

Live Monitoring shows the ongoing call's transcript in real time. Supervisors need an
on-demand, short handoff-context summary of the conversation so far. The LLM call must
be fault-tolerant: primary Gemini 3.1 Flash-Lite, fallback OpenAI GPT 5.4 mini, wrapped
under an abstraction reusable from anywhere in the codebase.

## Decisions (settled with the user)

1. **PHI boundary:** an OpenAI BAA exists — the OpenAI API joins the trust boundary as
   an in-boundary LLM. `vera-backend/CLAUDE.md`'s trust-boundary section MUST be updated
   in the same change (add "OpenAI API (LLM, BAA)" to the INSIDE list and relax the
   "only LLM is Vertex Gemini" phrasing to "only BAA-covered LLMs").
2. **Fallback machinery:** wrap LiveKit's `livekit.agents.llm.FallbackAdapter`
   (present in installed `livekit-agents==1.5.17`; signature
   `FallbackAdapter(llm: list[LLM], *, attempt_timeout=5.0, max_retry_per_llm=0,
   retry_interval=0.5, retry_on_chunk_sent=False)`) rather than hand-rolling retry /
   timeout / sequencing. LiveKit types never leak past the utility module.
3. **Caching:** summary responses cache in Redis for **5 seconds** (key per room).
   Repeated tab flips within 5s serve the cached summary; cost stays bounded, staleness
   is capped at 5s.
4. **Discoverability:** the utility is documented in the scoped
   `packages/vera_core/src/vera_core/CLAUDE.md` so future sessions reuse it instead of
   reinventing per-call-site LLM clients.

## Component 1 — `vera_core/llm.py` (new module)

Public surface (no LiveKit types cross it):

```python
@dataclass(frozen=True)
class LLMSpec:
    provider: str          # registry key: "google" | "openai" (extensible)
    model: str             # e.g. "gemini-3.1-flash-lite", "gpt-5.4-mini"
    extra: Mapping[str, Any] = ...   # provider-specific kwargs, optional

@dataclass(frozen=True)
class FallbackOptions:
    attempt_timeout: float = 8.0     # per-provider attempt budget
    max_retry_per_llm: int = 1
    retry_interval: float = 0.5

class LLMUnavailableError(Exception):
    """Every provider in the chain failed. Carries NO prompt/response text."""

class ResilientLLM:
    def __init__(self, primary: LLMSpec, fallbacks: Sequence[LLMSpec] = (),
                 options: FallbackOptions = FallbackOptions(),
                 secrets: SecretProvider | None = None) -> None: ...
    async def complete(self, *, system: str, user: str) -> str: ...
    async def aclose(self) -> None: ...
```

- **Provider registry:** module-level dict mapping provider key → factory
  `(LLMSpec, SecretProvider | None) -> livekit LLM`:
  - `"google"` → `livekit.plugins.google.LLM(model=spec.model, vertexai=True, **extra)`
    (same in-boundary Vertex path as the live pipeline; ADC/Workload Identity creds).
  - `"openai"` → `livekit.plugins.openai.LLM(model=spec.model, api_key=<OPENAI_API_KEY
    from SecretProvider>, **extra)`.
  Adding a provider later = one registry entry. Unknown provider → `ValueError` at
  construction time, not first use.
- **Lazy chain construction:** the underlying plugin LLMs and the `FallbackAdapter` are
  built on first `complete()` (LiveKit LLM clients open aiohttp sessions that need a
  running loop — same rule as `LiveKitGateway`). `aclose()` closes the chain.
- **`complete()`** builds a `ChatContext` (system + user message), drives
  `FallbackAdapter.chat(...)`, collects the streamed chunks, returns the joined string.
  Any terminal adapter failure (all providers exhausted) raises `LLMUnavailableError`.
- **PHI discipline:** the module never logs prompt or response text, and never logs
  exception reprs (provider errors can embed request payloads) — `type(exc).__name__`
  and provider/model labels only.
- **Dependencies:** `vera_core` gains `livekit-agents[google,openai]>=1.5.17`.
  (Control plane inherits it transitively; agent worker already pins these extras minus
  `openai` — the workspace lock unifies them.) New secret: `OPENAI_API_KEY` via
  `SecretProvider`; document in `env.example` and `adr/devops-todo.md`.
- **Tests** (`packages/vera_core/tests/unit/test_llm.py`): registry resolution +
  unknown-provider error; fallback semantics with stub LLM objects injected via the
  registry (primary raises → fallback answers; all raise → `LLMUnavailableError`);
  chunk collection.

## Component 2 — transcript snapshot + diarized formatting (control plane)

- `snapshot_transcript(...)` helper in a new `control_plane/call_summary.py` module
  (which also holds the formatter and prompt below): Redis first via
  `CallStreamService.read_all(room_name)` filtered to `TYPE_TRANSCRIPT` events; if the
  stream is gone, DB `Transcript` rows ordered by `seq` (the same redis-or-DB branch
  `stream_call_events` does inline today, factored to one place; the SSE endpoint may
  adopt it opportunistically but that refactor is not required for this slice).
- Pure formatter → diarized text, one line per turn, speaker from `source`:
  `rep` → `Payer rep:`, `bot` → `Vera (agent):`, `supervisor` → `Supervisor:`;
  DTMF turns render as `Vera (agent) [keypad]: <digits>`.

## Component 3 — endpoint `GET /api/v1/calls/{call_id}/summary`

Display-path chain, mirroring `stream_call_events` (`api/v1/calls.py:306`):

1. Authenticate (`current_identity`), tenant account required.
2. Authorize `calls:read`; same visibility rule as the events stream
   (`_call_hidden_from`).
3. Short-lived tenant session for the Call row (+ DB transcript fallback read).
4. Audit the PHI disclosure (same folded authz+PHI record shape as
   `stream_call_events`, `resource_type="call_summary"`).
5. Serve:
   - Redis cache probe: key `vera:summary:<room_name>`, TTL 5s (in-boundary
     Memorystore; PHI at rest allowed; wiped by TTL). Hit → return cached payload.
   - Miss → `snapshot_transcript`; fewer than 2 speech turns →
     `summary=None` + `status="pending"` ("not enough conversation yet"), no LLM call,
     not cached.
   - Otherwise → `ResilientLLM.complete(system=<handoff prompt>, user=<diarized
     transcript>)` → cache 5s → return.
6. `ResponseModel[CallSummaryResponse]`: `summary: str | None`,
   `status: "ready" | "pending"`, `generated_at: int` (epoch ms), `turn_count: int`.
   `Cache-Control: no-store`.
7. `LLMUnavailableError` → `CustomAPIException` with a 503-mapped code (add
   `SERVICE_UNAVAILABLE`-style `ExceptionCode` if none exists).

- **Prompt:** supervisor-handoff system prompt (brief; who the parties are, call
  purpose, progress so far, open items / next step). A module constant in
  `control_plane/call_summary.py` — `data/prompts/` is reserved for the call-pipeline
  prompt assets, and a one-endpoint prompt doesn't warrant that machinery.
- **Injection:** one `ResilientLLM` built at app startup from settings
  (`app.state.summary_llm`, `get_summary_llm` dep in `api/v1/common.py` — the
  `LiveKitGateway` pattern), closed on shutdown. Model selectors + cache TTL come from
  settings (`VERA_SUMMARY_PRIMARY_MODEL="google:gemini-3.1-flash-lite"`,
  `VERA_SUMMARY_FALLBACK_MODELS="openai:gpt-5.4-mini"`, `VERA_SUMMARY_CACHE_TTL_S=5`)
  so environments can retune without code.
- **Tests:** endpoint unit tests with a stubbed `ResilientLLM` + fake stores — authz
  denied, cache hit vs miss, pending (too-short transcript), LLM-unavailable → 503,
  DB-fallback path for a terminal call.

## Component 4 — documentation

- `vera-backend/CLAUDE.md`: trust boundary gains the OpenAI API (BAA-covered LLM).
- `packages/vera_core/src/vera_core/CLAUDE.md`: new section — any out-of-pipeline LLM
  call (summaries, analytics, extraction) MUST go through `vera_core.llm.ResilientLLM`
  with `LLMSpec` selectors; never instantiate provider SDK / LiveKit plugin LLM clients
  directly at call sites; never add a provider outside the registry.
- `env.example` + `adr/devops-todo.md`: `OPENAI_API_KEY` secret row.

## Error handling summary

| Failure | Behavior |
|---|---|
| Primary model error/timeout | FallbackAdapter moves to GPT 5.4 mini transparently |
| All providers fail | `LLMUnavailableError` → 503 envelope; no PHI in the error |
| No/short transcript | `status="pending"`, no LLM call |
| Call not found / hidden | 404, same shape as events endpoint |
| Redis cache down | Cache probe failures degrade to compute-fresh (log type name only) |

## Out of scope

- Frontend tabbed view (follow-up slice).
- Streaming the summary over SSE; auto-refresh push.
- Summarizing from inside the agent worker.
- Persisting summaries to Postgres.
