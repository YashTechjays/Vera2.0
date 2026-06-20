# Voice Pipeline — Core Cascade (Walking Skeleton) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a clickable end-to-end voice slice — set a patient `IN QUEUE` in the frontend, the control plane creates a `Call` + LiveKit room and dispatches the `vera-agent` worker, the call shows in Live Monitoring, and Intervene joins the room in the browser to talk to a Deepgram→Gemini→Cartesia agent.

**Architecture:** A LiveKit cascade worker (`agent_worker`) runs the STT→LLM→TTS pipeline with three Agent node-override seams that currently route through a no-op `PassthroughPHIBoundary` (so PHI tokenization drops in later untouched). The FastAPI control plane creates rooms, dispatches the agent, and mints browser join tokens. The Vite/React frontend connects to the room inside the existing `InterveneModal`.

**Tech Stack:** Python 3.12, livekit-agents 1.5.x (Deepgram/Google-Gemini/Cartesia/Silero/turn-detector plugins), livekit server SDK (`livekit-api`), FastAPI + SQLAlchemy async + Postgres (RLS), Vite + React 19 + TypeScript + `@livekit/components-react`.

**Spec:** `docs/superpowers/specs/2026-06-19-voice-pipeline-core-cascade-design.md`
**Reference POC (latency knobs + persona):** `/Users/tapusd/src/tries/2026-05-18-vera-2-client-demo-cascading-vs-audio-native-model`
**Frontend repo (separate):** `/Users/tapusd/Work/Techjays/Vera/vera-frontend`

## Global Constraints

- **Python pinned `>=3.12,<3.13`.** PEP 695 type params only (`class Foo[T]`, `def f[T]`) — ruff rejects `Generic[T]`/`TypeVar`.
- **asyncio only — never import `anyio`** and never add it to a `pyproject.toml` `dependencies`. Use stdlib `asyncio.TaskGroup`/`asyncio.timeout`.
- **CI gate is `just check`** = `lint` (ruff format check + ruff check) + `typecheck` (mypy, strict) + `test` (pytest). Must be green before "done".
- **Vendored `packages/phi_codec` is excluded from ruff & mypy** — do not touch it; integrate only at the `vera_core.phi` boundary.
- **Commit messages: do NOT add `Co-Authored-By: Claude`** (user global rule). Conventional-commit prefixes (`feat:`/`test:`/`chore:`).
- **Reuse `vera_core.observability.correlation` verbatim** (`room_name_for_call`, `parse_room_name`, `call_trace_attributes`) — never invent a new room/session id scheme.
- **DB rules:** UUIDv7 PKs are client-generated on `flush()` (no DB round-trip). `created_at`/`updated_at` come from Postgres `now()` — never set from Python. Always do tenant work inside a `tenant_session`/`TenantSession` dependency (RLS).
- **SYNTHETIC DATA ONLY.** No tokenization exists yet, so no real PHI may be carried on any call until the codec lands (a later spec).
- **livekit-agents 1.5.x** emits v2.0 deprecation warnings for `min/max_endpointing_delay`, `preemptive_generation`, `allow_interruptions`, `turn_detection` — they still work; do NOT "fix" them.
- **Frontend:** Vite + React 19 SPA, TypeScript, Tailwind v4, radix-ui primitives in `src/components/ui/`, path alias `@` → `./src`. New backend calls follow the `VITE_API_URL` swap-point convention already noted in `src/lib/ibv/mock.ts`.

---

## File Structure

**`vera-backend`:**
- `docker-compose.yml` (modify) — add `livekit` service.
- `apps/agent_worker/pyproject.toml` (modify) — add LiveKit plugin extras.
- `apps/agent_worker/src/agent_worker/prompt.py` (create) — persona text + assembly.
- `apps/agent_worker/src/agent_worker/seams.py` (create) — testable PHI-seam helpers.
- `apps/agent_worker/src/agent_worker/cascade.py` (create) — `cascade_session_kwargs` + `build_session`.
- `apps/agent_worker/src/agent_worker/agent.py` (create) — `VeraAgent(Agent)` node overrides.
- `apps/agent_worker/src/agent_worker/main.py` (modify) — entrypoint lifecycle + prewarm.
- `apps/agent_worker/tests/...` (create) — worker unit tests.
- `packages/vera_core/src/vera_core/phi/protocol.py` (create) — `PHIBoundaryProtocol`, `PassthroughPHIBoundary`.
- `packages/vera_core/src/vera_core/phi/factory.py` (create) — `build_phi_boundary`.
- `packages/vera_core/src/vera_core/phi/__init__.py` (modify) — export the above.
- `packages/vera_core/src/vera_core/schemas/dto.py` (modify) — grow `CallSummary`, add `StartCallRequest`, `JoinTokenResponse`.
- `packages/vera_core/src/vera_core/config/settings.py` (modify) — add `livekit_url`.
- `apps/control_plane/pyproject.toml` (modify) — add `livekit-api`.
- `apps/control_plane/src/control_plane/livekit_gateway.py` (create) — room/dispatch/token wrapper + `build_livekit_gateway`.
- `apps/control_plane/src/control_plane/api/v1/calls.py` (modify) — three real endpoints.
- `apps/control_plane/src/control_plane/deps.py` (modify) — `get_livekit` accessor.
- `apps/control_plane/src/control_plane/main.py` (modify) — wire `livekit` + secrets into `app.state`, call `configure_observability`.
- `tests/integration/control_plane/test_calls.py` (create) — endpoint tests.

**`vera-frontend`:**
- `package.json` (modify) — add `livekit-client`, `@livekit/components-react`, `@livekit/components-styles`.
- `src/lib/api/client.ts` (create) — base fetch + `VITE_API_URL` + dev bearer.
- `src/lib/api/calls.ts` (create) — `startCall`, `getJoinToken`, `listActiveCalls`.
- `src/lib/mock-data.ts` (modify) — extend `LiveCall` with `callId?`/`roomName?`.
- `src/pages/DataManagement.tsx` + `src/components/data-management/RecordFormModal.tsx` (modify) — IN QUEUE → `startCall`.
- `src/pages/LiveMonitoring.tsx` (modify) — Intervene → `getJoinToken` → pass token to modal.
- `src/components/monitoring/InterveneModal.tsx` (modify) — render `LiveKitRoom` + audio + transcript.
- `src/lib/api/calls.test.ts` (create) — vitest.

---

# Phase 1 — Local infra & worker deps

### Task 1: LiveKit server in docker-compose + worker plugin deps

**Files:**
- Modify: `docker-compose.yml`
- Modify: `apps/agent_worker/pyproject.toml`

**Interfaces:**
- Produces: a reachable local LiveKit at `ws://localhost:7880` with dev key/secret `devkey`/`secret`; worker importable with STT/LLM/TTS plugins.

- [ ] **Step 1: Add the `livekit` service to `docker-compose.yml`** (append under `services:`, keep existing postgres/redis/sendria):

```yaml
  livekit:
    image: livekit/livekit-server:latest
    command: --dev --bind 0.0.0.0 --node-ip 127.0.0.1
    ports:
      - "7880:7880"
      - "7881:7881"
      - "7882:7882/udp"
```

Rationale (do not omit the flags): without `--bind 0.0.0.0` LiveKit listens on container loopback only and the port-forward can't reach it; without `--node-ip 127.0.0.1` it advertises its container IP as the ICE candidate and the browser hangs in "checking" forever. `--dev` enables the well-known dev key `devkey` / secret `secret`.

- [ ] **Step 2: Verify compose parses**

Run: `docker compose config >/dev/null && echo OK`
Expected: `OK`

- [ ] **Step 3: Add plugin extras to `apps/agent_worker/pyproject.toml`** — replace the `livekit-agents>=1.5.17` line in `[project].dependencies` with:

```toml
    "livekit-agents[deepgram,google,cartesia,silero,turn-detector]>=1.5.17",
```

- [ ] **Step 4: Sync and verify imports**

Run: `uv sync --all-packages && uv run python -c "from livekit.plugins import deepgram, google, cartesia, silero; from livekit.plugins.turn_detector.english import EnglishModel; print('plugins OK')"`
Expected: `plugins OK`

- [ ] **Step 5: Pre-download the turn-detector model** (one-time; otherwise the first turn of every call stalls)

Run: `uv run python -m livekit.agents download-files`
Expected: downloads complete, exit 0.

- [ ] **Step 6: Bring up infra and confirm LiveKit is live**

Run: `just up && curl -fsS http://localhost:7880 >/dev/null && echo "livekit up"`
Expected: `livekit up` (LiveKit returns 200 on `/`).

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml apps/agent_worker/pyproject.toml uv.lock
git commit -m "chore(infra): add local LiveKit server + agent-worker STT/LLM/TTS plugin deps"
```

---

# Phase 2 — Inert PHI/transcript seam (`vera_core`)

### Task 2: `PHIBoundaryProtocol` + `PassthroughPHIBoundary`

**Files:**
- Create: `packages/vera_core/src/vera_core/phi/protocol.py`
- Test: `packages/vera_core/tests/unit/phi/test_passthrough.py`
- Modify: `packages/vera_core/src/vera_core/phi/__init__.py`

**Interfaces:**
- Produces:
  - `PHIBoundaryProtocol` — `Protocol` with `async open_session(session_id: str, known: dict[str, str | list[str]] | None = None) -> None`, `async close_session(session_id: str) -> None`, `async redact(session_id: str, text: str) -> str`, `async hydrate_for_speech(session_id: str, text: str) -> str`, `async hydrate_raw(session_id: str, args: dict[str, Any]) -> dict[str, Any]`. (Matches the real `PHIBoundary` shape in `phi/boundary.py` so the real class satisfies it structurally.)
  - `PassthroughPHIBoundary` — concrete no-op implementation.

- [ ] **Step 1: Write the failing test** (`test_passthrough.py`):

```python
import pytest

from vera_core.phi import PassthroughPHIBoundary, PHIBoundaryProtocol


@pytest.mark.asyncio
async def test_passthrough_is_identity_and_satisfies_protocol() -> None:
    b = PassthroughPHIBoundary()
    assert isinstance(b, PHIBoundaryProtocol)

    await b.open_session("s1", {"name": "Jane"})  # accepted, no-op
    assert await b.redact("s1", "Jane Doe, member 123") == "Jane Doe, member 123"
    assert await b.hydrate_for_speech("s1", "[[NAME_1]]") == "[[NAME_1]]"
    assert await b.hydrate_raw("s1", {"member": "[[ID_1]]"}) == {"member": "[[ID_1]]"}
    await b.close_session("s1")  # no-op, must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/vera_core/tests/unit/phi/test_passthrough.py -v`
Expected: FAIL — `ImportError` (`PassthroughPHIBoundary` not defined).

- [ ] **Step 3: Implement `protocol.py`**

```python
"""Swappable PHI-boundary seam.

The cascade depends only on `PHIBoundaryProtocol`. Today the factory returns the
no-op `PassthroughPHIBoundary` (tokenization is not yet wired). When the codec
lands, the real `vera_core.phi.boundary.PHIBoundary` — which already has this
exact method shape — is returned instead, with zero cascade changes.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PHIBoundaryProtocol(Protocol):
    async def open_session(
        self, session_id: str, known: dict[str, str | list[str]] | None = None
    ) -> None: ...
    async def close_session(self, session_id: str) -> None: ...
    async def redact(self, session_id: str, text: str) -> str: ...
    async def hydrate_for_speech(self, session_id: str, text: str) -> str: ...
    async def hydrate_raw(
        self, session_id: str, args: dict[str, Any]
    ) -> dict[str, Any]: ...


class PassthroughPHIBoundary:
    """No-op boundary: text flows through unchanged. Synthetic data only."""

    async def open_session(
        self, session_id: str, known: dict[str, str | list[str]] | None = None
    ) -> None:
        return None

    async def close_session(self, session_id: str) -> None:
        return None

    async def redact(self, session_id: str, text: str) -> str:
        return text

    async def hydrate_for_speech(self, session_id: str, text: str) -> str:
        return text

    async def hydrate_raw(
        self, session_id: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        return args
```

- [ ] **Step 4: Export from `phi/__init__.py`** — add:

```python
from vera_core.phi.protocol import PassthroughPHIBoundary, PHIBoundaryProtocol
```

and add both names to `__all__` if the file defines one.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest packages/vera_core/tests/unit/phi/test_passthrough.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/phi/protocol.py packages/vera_core/src/vera_core/phi/__init__.py packages/vera_core/tests/unit/phi/test_passthrough.py
git commit -m "feat(phi): add PHIBoundaryProtocol + PassthroughPHIBoundary seam"
```

### Task 3: `build_phi_boundary` factory

**Files:**
- Create: `packages/vera_core/src/vera_core/phi/factory.py`
- Test: `packages/vera_core/tests/unit/phi/test_factory.py`
- Modify: `packages/vera_core/src/vera_core/phi/__init__.py`

**Interfaces:**
- Consumes: `PHIBoundaryProtocol`, `PassthroughPHIBoundary` (Task 2); `Settings`.
- Produces: `def build_phi_boundary(settings: "Settings") -> PHIBoundaryProtocol` — returns `PassthroughPHIBoundary()` for now (mirrors `build_kms`). The worker calls this once per process.

- [ ] **Step 1: Write the failing test** (`test_factory.py`):

```python
from vera_core.config.settings import Settings
from vera_core.phi import PassthroughPHIBoundary, build_phi_boundary


def test_build_phi_boundary_returns_passthrough_for_now() -> None:
    settings = Settings(_env_file=None)
    boundary = build_phi_boundary(settings)
    assert isinstance(boundary, PassthroughPHIBoundary)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest packages/vera_core/tests/unit/phi/test_factory.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `factory.py`**

```python
"""Factory for the PHI boundary (mirrors config.kms.build_kms).

Returns PassthroughPHIBoundary today. When tokenization lands, this is where the
real PHIBoundary + the `phi_tokenizer_disabled` flag + the prod hard-fail guard
will be selected — the only place that branches on config.
"""

from typing import TYPE_CHECKING

from vera_core.phi.protocol import PassthroughPHIBoundary, PHIBoundaryProtocol

if TYPE_CHECKING:
    from vera_core.config.settings import Settings


def build_phi_boundary(settings: "Settings") -> PHIBoundaryProtocol:
    # TODO(vera-2.x): when the codec is wired, return the real PHIBoundary unless
    # phi_tokenizer_disabled is set; hard-fail if disabled in prod.
    return PassthroughPHIBoundary()
```

- [ ] **Step 4: Export from `phi/__init__.py`** — add `from vera_core.phi.factory import build_phi_boundary` (+ `__all__`).

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest packages/vera_core/tests/unit/phi/test_factory.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/phi/factory.py packages/vera_core/src/vera_core/phi/__init__.py packages/vera_core/tests/unit/phi/test_factory.py
git commit -m "feat(phi): add build_phi_boundary factory"
```

---

# Phase 3 — Agent worker

### Task 4: Agent persona (`prompt.py`)

**Files:**
- Create: `apps/agent_worker/src/agent_worker/prompt.py`
- Test: `apps/agent_worker/tests/unit/test_prompt.py`

**Interfaces:**
- Produces: `SYSTEM_PROMPT: str`, `GREETING: str`, `CARTESIA_MARKUP_GUIDE: str`, `def build_instructions() -> str` (returns `SYSTEM_PROMPT + "\n\n" + CARTESIA_MARKUP_GUIDE`).

> Source the `SYSTEM_PROMPT` and `GREETING` text **verbatim** from the POC `backend/app/agent/prompt.py` (lines 9-85), then **adapt for chat-only**: delete the entire `TOOL USE` section and every `record_service_coverage` / `end_call` reference; where the prompt said to "call end_call", replace with "say a brief polite closing line such as 'thanks so much for your help, have a good one' and stop." Keep PERSONA, CORE OBJECTIVE, both gates, the CPT/ICD service lists, ADAPTIVE DATA COLLECTION, and CONVERSATION STYLE. Copy `CARTESIA_MARKUP_GUIDE` verbatim from POC `prompt.py:88-94`.

- [ ] **Step 1: Write the failing test** (`test_prompt.py`):

```python
from agent_worker.prompt import (
    CARTESIA_MARKUP_GUIDE,
    GREETING,
    SYSTEM_PROMPT,
    build_instructions,
)


def test_prompt_is_chat_only_and_includes_cartesia_guide() -> None:
    # chat-only: no tool machinery leaked into the prompt
    assert "record_service_coverage" not in SYSTEM_PROMPT
    assert "end_call" not in SYSTEM_PROMPT
    # persona + objective retained
    assert "infertility" in SYSTEM_PROMPT.lower()
    assert "diagnostic testing" in SYSTEM_PROMPT.lower()
    # greeting is the outbound opener
    assert GREETING.startswith("Hi, I'm calling on behalf of a patient")
    # assembly appends the Cartesia guide
    combined = build_instructions()
    assert combined.startswith(SYSTEM_PROMPT)
    assert CARTESIA_MARKUP_GUIDE in combined
    assert "<spell>" in CARTESIA_MARKUP_GUIDE
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest apps/agent_worker/tests/unit/test_prompt.py -v`
Expected: FAIL — `ModuleNotFoundError: agent_worker.prompt`.

- [ ] **Step 3: Implement `prompt.py`** — paste the adapted `SYSTEM_PROMPT`, `GREETING`, and `CARTESIA_MARKUP_GUIDE` strings (per the source note above), then:

```python
def build_instructions() -> str:
    """Chat-only instructions: persona + Cartesia readback guide (we use sonic-3.5)."""
    return f"{SYSTEM_PROMPT}\n\n{CARTESIA_MARKUP_GUIDE}"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest apps/agent_worker/tests/unit/test_prompt.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/prompt.py apps/agent_worker/tests/unit/test_prompt.py
git commit -m "feat(worker): add chat-only infertility verification persona + Cartesia guide"
```

### Task 5: PHI-seam helpers (`seams.py`)

**Files:**
- Create: `apps/agent_worker/src/agent_worker/seams.py`
- Test: `apps/agent_worker/tests/unit/test_seams.py`

**Interfaces:**
- Consumes: `PHIBoundaryProtocol` (Task 2); `livekit.agents.stt.SpeechEvent`, `SpeechEventType`.
- Produces:
  - `async def redact_event(boundary: PHIBoundaryProtocol, session_id: str, ev: SpeechEvent) -> SpeechEvent` — for FINAL/PREFLIGHT events, replaces `ev.alternatives[0].text` with `await boundary.redact(session_id, text)`; returns `ev`. (Preemptive-generation-safe: PREFLIGHT is covered. INTERIM is left untouched — we don't show interim captions.)
  - `async def hydrate_stream(boundary: PHIBoundaryProtocol, session_id: str, text: AsyncIterable[str]) -> AsyncIterator[str]` — yields each chunk run through `boundary.hydrate_for_speech`. (A later task swaps in `SpeechStreamHydrator`; for the inert seam, per-chunk identity is sufficient and testable.)

> These helpers isolate the PHI wall from the LiveKit node plumbing so they are unit-testable without a running `AgentSession`. With `PassthroughPHIBoundary` they are identity; the test proves both delegation (spy) and identity (passthrough).

- [ ] **Step 1: Write the failing test** (`test_seams.py`):

```python
from typing import Any

import pytest
from livekit.agents import stt

from agent_worker.seams import hydrate_stream, redact_event
from vera_core.phi import PassthroughPHIBoundary


class SpyBoundary(PassthroughPHIBoundary):
    def __init__(self) -> None:
        self.redacted: list[str] = []

    async def redact(self, session_id: str, text: str) -> str:
        self.redacted.append(text)
        return f"[redacted:{text}]"


def _event(kind: stt.SpeechEventType, text: str) -> stt.SpeechEvent:
    return stt.SpeechEvent(
        type=kind,
        alternatives=[stt.SpeechData(language="en", text=text)],
    )


@pytest.mark.asyncio
async def test_redact_event_rewrites_final_and_preflight() -> None:
    spy = SpyBoundary()
    final = await redact_event(spy, "s1", _event(stt.SpeechEventType.FINAL_TRANSCRIPT, "Jane Doe"))
    assert final.alternatives[0].text == "[redacted:Jane Doe]"
    assert spy.redacted == ["Jane Doe"]


@pytest.mark.asyncio
async def test_redact_event_skips_interim() -> None:
    spy = SpyBoundary()
    interim = await redact_event(spy, "s1", _event(stt.SpeechEventType.INTERIM_TRANSCRIPT, "Ja"))
    assert interim.alternatives[0].text == "Ja"  # untouched
    assert spy.redacted == []


@pytest.mark.asyncio
async def test_hydrate_stream_passthrough_is_identity() -> None:
    async def gen() -> Any:
        for c in ["Hello ", "[[NAME_1]]"]:
            yield c

    out = [c async for c in hydrate_stream(PassthroughPHIBoundary(), "s1", gen())]
    assert "".join(out) == "Hello [[NAME_1]]"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest apps/agent_worker/tests/unit/test_seams.py -v`
Expected: FAIL — `ModuleNotFoundError: agent_worker.seams`.

> If `stt.SpeechEvent`/`SpeechData` construction differs in the installed plugin version, confirm the constructor via `uv run python -c "from livekit.agents import stt; help(stt.SpeechEvent)"` and adjust the `_event` helper. The PREFLIGHT enum member is `stt.SpeechEventType.PREFLIGHT_TRANSCRIPT`.

- [ ] **Step 3: Implement `seams.py`**

```python
"""PHI-wall seam helpers, isolated from LiveKit node plumbing for testability.

Today the boundary is PassthroughPHIBoundary (identity). The placements match the
POC's validated, preemptive-generation-safe design: redact FINAL+PREFLIGHT (never
on_user_turn_completed, which misses PREFLIGHT); hydrate the TTS-bound text only.
"""

from collections.abc import AsyncIterable, AsyncIterator

from livekit.agents import stt

from vera_core.phi import PHIBoundaryProtocol

_REDACT_TYPES = {
    stt.SpeechEventType.FINAL_TRANSCRIPT,
    stt.SpeechEventType.PREFLIGHT_TRANSCRIPT,
}


async def redact_event(
    boundary: PHIBoundaryProtocol, session_id: str, ev: stt.SpeechEvent
) -> stt.SpeechEvent:
    if ev.type in _REDACT_TYPES and ev.alternatives:
        alt = ev.alternatives[0]
        alt.text = await boundary.redact(session_id, alt.text)
    return ev


async def hydrate_stream(
    boundary: PHIBoundaryProtocol, session_id: str, text: AsyncIterable[str]
) -> AsyncIterator[str]:
    async for chunk in text:
        yield await boundary.hydrate_for_speech(session_id, chunk)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest apps/agent_worker/tests/unit/test_seams.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/seams.py apps/agent_worker/tests/unit/test_seams.py
git commit -m "feat(worker): add testable PHI-seam helpers (redact FINAL+PREFLIGHT, hydrate TTS text)"
```

### Task 6: Cascade builder (`cascade.py`)

**Files:**
- Create: `apps/agent_worker/src/agent_worker/cascade.py`
- Test: `apps/agent_worker/tests/unit/test_cascade.py`

**Interfaces:**
- Produces:
  - `def cascade_session_kwargs() -> dict[str, Any]` — pure dict of the latency-tuned `AgentSession` knobs (no plugin construction), so the tuning is unit-testable.
  - `def build_session(vad: Any | None = None) -> AgentSession` — constructs the Deepgram→Gemini→Cartesia `AgentSession` using `cascade_session_kwargs()`; accepts an injected prewarmed `vad`.

- [ ] **Step 1: Write the failing test** (`test_cascade.py`) — assert the tuned knobs (the deterministic, high-value part):

```python
from agent_worker.cascade import cascade_session_kwargs


def test_cascade_latency_knobs() -> None:
    kw = cascade_session_kwargs()
    assert kw["preemptive_generation"] is True
    assert kw["min_endpointing_delay"] == 0.3
    assert kw["max_endpointing_delay"] == 0.6
    assert kw["allow_interruptions"] is True
    assert kw["min_interruption_duration"] == 0.5
    assert kw["false_interruption_timeout"] == 2.0
    assert kw["resume_false_interruption"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest apps/agent_worker/tests/unit/test_cascade.py -v`
Expected: FAIL — `ModuleNotFoundError: agent_worker.cascade`.

- [ ] **Step 3: Implement `cascade.py`**

```python
"""Deepgram(Flux) -> Gemini -> Cartesia cascade, latency-tuned per the POC.

The biggest wins: preemptive_generation fed by Deepgram Flux's eager EOT, and
Gemini thinking_budget=0. Keep EnglishModel turn detection — dropping it falls
back to dumb VAD-silence detection, which is worse.
"""

from typing import Any

from google.genai.types import ThinkingConfig
from livekit.agents import AgentSession
from livekit.plugins import cartesia, deepgram, google, silero
from livekit.plugins.turn_detector.english import EnglishModel


def cascade_session_kwargs() -> dict[str, Any]:
    return {
        "preemptive_generation": True,
        "min_endpointing_delay": 0.3,
        "max_endpointing_delay": 0.6,
        "allow_interruptions": True,
        "min_interruption_duration": 0.5,
        "false_interruption_timeout": 2.0,
        "resume_false_interruption": True,
    }


def build_session(vad: Any | None = None) -> AgentSession:
    return AgentSession(
        stt=deepgram.STTv2(model="flux-general-en", eager_eot_threshold=0.5),
        llm=google.LLM(
            model="gemini-2.5-flash",
            thinking_config=ThinkingConfig(thinking_budget=0),
        ),
        tts=cartesia.TTS(model="sonic-3.5", emotion=["confident"]),
        vad=vad or silero.VAD.load(min_silence_duration=0.4),
        turn_detection=EnglishModel(),
        **cascade_session_kwargs(),
    )
```

> Verify provider constructor signatures against the installed plugin versions before relying on `build_session` at runtime (e.g. `cartesia.TTS` `emotion` param shape, `deepgram.STTv2` kwargs, `google.LLM` thinking config). The `cascade_session_kwargs` test is the deterministic gate; `build_session` is exercised in the manual run (Task 17).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest apps/agent_worker/tests/unit/test_cascade.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/cascade.py apps/agent_worker/tests/unit/test_cascade.py
git commit -m "feat(worker): add latency-tuned Deepgram->Gemini->Cartesia cascade builder"
```

### Task 7: `VeraAgent` with node-override seams (`agent.py`)

**Files:**
- Create: `apps/agent_worker/src/agent_worker/agent.py`
- Test: `apps/agent_worker/tests/unit/test_agent.py`

**Interfaces:**
- Consumes: `build_instructions`/`GREETING` (Task 4); `redact_event`/`hydrate_stream` (Task 5); `PHIBoundaryProtocol` (Task 2).
- Produces: `class VeraAgent(Agent)` — `__init__(self, boundary: PHIBoundaryProtocol, session_id: str)`; overrides `stt_node`, `transcription_node`, `tts_node`; speaks `GREETING` on `on_enter`. `tools=[]` (chat-only).

- [ ] **Step 1: Write the failing test** (`test_agent.py`) — assert construction is chat-only and carries the persona:

```python
from agent_worker.agent import VeraAgent
from vera_core.phi import PassthroughPHIBoundary


def test_vera_agent_is_chat_only_with_persona() -> None:
    agent = VeraAgent(boundary=PassthroughPHIBoundary(), session_id="s1")
    # chat-only: no tools registered
    assert list(agent.tools) == []
    # persona instructions are attached
    assert "infertility" in agent.instructions.lower()
```

> Confirm the public accessors for tools/instructions on the installed `Agent` base (`agent.tools`, `agent.instructions`); if they differ, adjust the assertions. Use `uv run python -c "from livekit.agents import Agent; print([a for a in dir(Agent) if not a.startswith('_')])"`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest apps/agent_worker/tests/unit/test_agent.py -v`
Expected: FAIL — `ModuleNotFoundError: agent_worker.agent`.

- [ ] **Step 3: Implement `agent.py`**

```python
"""VeraAgent — the cascade agent with inert PHI-wall node overrides.

stt_node: redact FINAL+PREFLIGHT before the LLM (preemptive-safe).
transcription_node: pass-through (future tokenized-transcript tap).
tts_node: hydrate the TTS-bound text only (audio stays the only PHI surface).
All three route through PHIBoundaryProtocol — today PassthroughPHIBoundary (no-op).
"""

from collections.abc import AsyncIterable
from typing import Any

from livekit.agents import Agent, ModelSettings, stt
from livekit.agents.voice.agent import ModelSettings as _MS  # noqa: F401 (version guard)

from agent_worker.prompt import GREETING, build_instructions
from agent_worker.seams import hydrate_stream, redact_event
from vera_core.phi import PHIBoundaryProtocol


class VeraAgent(Agent):
    def __init__(self, boundary: PHIBoundaryProtocol, session_id: str) -> None:
        self._boundary = boundary
        self._session_id = session_id
        super().__init__(instructions=build_instructions(), tools=[])

    async def on_enter(self) -> None:
        self.session.say(GREETING)

    async def stt_node(self, audio: Any, model_settings: ModelSettings) -> Any:
        async for ev in Agent.default.stt_node(self, audio, model_settings):
            if isinstance(ev, stt.SpeechEvent):
                ev = await redact_event(self._boundary, self._session_id, ev)
            yield ev

    def transcription_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterable[str]:
        # Pass-through today; future: tap tokenized assistant segments here.
        return Agent.default.transcription_node(self, text, model_settings)

    def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> Any:
        hydrated = hydrate_stream(self._boundary, self._session_id, text)
        return Agent.default.tts_node(self, hydrated, model_settings)
```

> The `Agent.default.<node>` delegation pattern + `on_enter`/`session.say` are the documented 1.5.x hooks (confirmed via the POC and LiveKit docs). Remove the `_MS` version-guard import if mypy/ruff flags it; it's only there as a reminder to confirm `ModelSettings`' import path in the installed version.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest apps/agent_worker/tests/unit/test_agent.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/agent.py apps/agent_worker/tests/unit/test_agent.py
git commit -m "feat(worker): add VeraAgent with inert PHI-wall node overrides"
```

### Task 8: Entrypoint lifecycle + prewarm (`main.py`)

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/main.py`
- Test: `apps/agent_worker/tests/unit/test_main_session_id.py`

**Interfaces:**
- Consumes: `build_session` (Task 6), `VeraAgent` (Task 7), `build_phi_boundary` (Task 3), `parse_room_name`/`call_trace_attributes` (correlation), `get_settings`.
- Produces: replaces the echo entrypoint with the cascade lifecycle; `prewarm(proc)` loads Silero VAD; `def session_id_for(room_name: str) -> str` (the room name is the session id — small testable helper).

- [ ] **Step 1: Write the failing test** (`test_main_session_id.py`):

```python
from agent_worker.main import session_id_for


def test_session_id_is_room_name() -> None:
    room = "call--11111111-1111-7111-8111-111111111111--22222222-2222-7222-8222-222222222222"
    assert session_id_for(room) == room
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest apps/agent_worker/tests/unit/test_main_session_id.py -v`
Expected: FAIL — `ImportError: cannot import name 'session_id_for'`.

- [ ] **Step 3: Rewrite `main.py`** — replace the echo skeleton (keep `AGENT_NAME = "vera-agent"` and `build_worker_options`/`cli.run_app`):

```python
"""Vera agent worker — Deepgram->Gemini->Cartesia cascade over LiveKit.

Explicit dispatch only (agent_name set): the control plane dispatches this worker
into a room named by vera_core.observability.correlation.room_name_for_call. The
room name IS the session id and the Langfuse correlation key.
"""

import logging

from livekit.agents import JobContext, WorkerOptions, cli
from livekit.plugins import silero

from agent_worker.agent import VeraAgent
from agent_worker.cascade import build_session
from vera_core.config.settings import get_settings
from vera_core.observability.correlation import call_trace_attributes, parse_room_name
from vera_core.phi import build_phi_boundary

logger = logging.getLogger("agent_worker")

AGENT_NAME = "vera-agent"


def session_id_for(room_name: str) -> str:
    """The room name is the session id (correlation key shared with the control plane)."""
    return room_name


def prewarm(proc: "cli.JobProcess") -> None:  # type: ignore[name-defined]
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=0.4)


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    room_name = ctx.room.name
    room_ref = parse_room_name(room_name)
    if room_ref is None:
        logger.warning("foreign room name %s — not a vera call room", room_name)
        return
    session_id = session_id_for(room_name)

    settings = get_settings()
    boundary = build_phi_boundary(settings)
    await boundary.open_session(session_id)

    session = build_session(vad=ctx.proc.userdata.get("vad"))
    # Span attributes group every pipeline span under langfuse.session.id = room name.
    _ = call_trace_attributes(room_name)

    async def _on_shutdown() -> None:
        await boundary.close_session(session_id)

    ctx.add_shutdown_callback(_on_shutdown)
    await session.start(
        agent=VeraAgent(boundary=boundary, session_id=session_id), room=ctx.room
    )


def build_worker_options() -> WorkerOptions:
    return WorkerOptions(
        entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name=AGENT_NAME
    )


if __name__ == "__main__":
    cli.run_app(build_worker_options())
```

> Confirm `call_trace_attributes` is applied the way the codebase intends (attach to the current span) — check `vera_core/observability/correlation.py` usage; if there's a helper to set them on the active span, call it instead of discarding into `_`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest apps/agent_worker/tests/unit/test_main_session_id.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full worker test suite + lint/type**

Run: `uv run pytest apps/agent_worker/tests/ -v && just lint && just typecheck`
Expected: all PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add apps/agent_worker/src/agent_worker/main.py apps/agent_worker/tests/unit/test_main_session_id.py
git commit -m "feat(worker): replace echo skeleton with cascade lifecycle + VAD prewarm"
```

---

# Phase 4 — Control plane

### Task 9: LiveKit gateway (room create + dispatch + token mint)

**Files:**
- Modify: `packages/vera_core/src/vera_core/config/settings.py` (add `livekit_url`)
- Modify: `apps/control_plane/pyproject.toml` (add `livekit-api`)
- Create: `apps/control_plane/src/control_plane/livekit_gateway.py`
- Test: `tests/integration/control_plane/test_livekit_gateway.py`

**Interfaces:**
- Produces:
  - `Settings.livekit_url: str | None = None` (env `VERA_LIVEKIT_URL`).
  - `class LiveKitGateway` — `__init__(self, url: str, api_key: str, api_secret: str)`; `async create_call_room(room_name: str) -> None` (create room + `agent_dispatch.create_dispatch("vera-agent", room_name)`); `def mint_join_token(room_name: str, identity: str) -> str`.
  - `def build_livekit_gateway(settings: Settings, secrets: SecretProvider) -> LiveKitGateway` — reads `settings.livekit_url` + secrets `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET`.

- [ ] **Step 1: Add `livekit-api>=1.0` to `apps/control_plane/pyproject.toml` dependencies, then `uv sync --all-packages`.**

- [ ] **Step 2: Add the settings field** — in `settings.py`, near the observability block:

```python
    livekit_url: str | None = None
```

- [ ] **Step 3: Write the failing test** (`test_livekit_gateway.py`) — token minting is deterministic and verifiable by decoding the JWT:

```python
import jwt

from control_plane.livekit_gateway import LiveKitGateway


def test_mint_join_token_grants_room_join() -> None:
    gw = LiveKitGateway(url="ws://localhost:7880", api_key="devkey", api_secret="secret")
    token = gw.mint_join_token(room_name="call--t--c", identity="supervisor-1")

    claims = jwt.decode(token, "secret", algorithms=["HS256"])
    assert claims["sub"] == "supervisor-1"
    assert claims["video"]["room"] == "call--t--c"
    assert claims["video"]["roomJoin"] is True
```

- [ ] **Step 4: Run to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_livekit_gateway.py -v`
Expected: FAIL — `ModuleNotFoundError: control_plane.livekit_gateway`.

- [ ] **Step 5: Implement `livekit_gateway.py`**

```python
"""Thin wrapper over the LiveKit server SDK: create call rooms, dispatch the
agent worker, and mint browser join tokens. Mirrors the build_kms factory shape.
"""

from livekit import api

from vera_core.config import SecretProvider
from vera_core.config.settings import Settings

AGENT_NAME = "vera-agent"


class LiveKitGateway:
    def __init__(self, url: str, api_key: str, api_secret: str) -> None:
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret

    @property
    def url(self) -> str:
        return self._url

    async def create_call_room(self, room_name: str) -> None:
        lk = api.LiveKitAPI(self._url, self._api_key, self._api_secret)
        try:
            await lk.room.create_room(api.CreateRoomRequest(name=room_name))
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room_name)
            )
        finally:
            await lk.aclose()

    def mint_join_token(self, room_name: str, identity: str) -> str:
        grants = api.VideoGrants(room_join=True, room=room_name)
        return (
            api.AccessToken(self._api_key, self._api_secret)
            .with_identity(identity)
            .with_grants(grants)
            .to_jwt()
        )


def build_livekit_gateway(settings: Settings, secrets: SecretProvider) -> LiveKitGateway:
    if settings.livekit_url is None:
        raise ValueError("VERA_LIVEKIT_URL must be set to use the LiveKit gateway")
    return LiveKitGateway(
        url=settings.livekit_url,
        api_key=secrets.get("LIVEKIT_API_KEY"),
        api_secret=secrets.get("LIVEKIT_API_SECRET"),
    )
```

> Confirm `api.VideoGrants`/`AccessToken` builder method names against the installed `livekit-api` (`with_identity`/`with_grants`/`to_jwt` are the current API). If `create_room`/`agent_dispatch` live under different attributes, verify with `uv run python -c "from livekit import api; print([a for a in dir(api) if not a.startswith('_')])"`.

- [ ] **Step 6: Run to verify it passes**

Run: `uv run pytest tests/integration/control_plane/test_livekit_gateway.py -v`
Expected: PASS. (`PyJWT` ships transitively with `livekit-api`; if missing, add `pyjwt` to control-plane dev deps.)

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/config/settings.py apps/control_plane/pyproject.toml apps/control_plane/src/control_plane/livekit_gateway.py tests/integration/control_plane/test_livekit_gateway.py uv.lock
git commit -m "feat(control-plane): add LiveKit gateway (room create, agent dispatch, join-token mint)"
```

### Task 10: Grow `CallSummary` + request/response DTOs

**Files:**
- Modify: `packages/vera_core/src/vera_core/schemas/dto.py`
- Test: `packages/vera_core/tests/unit/schemas/test_call_dtos.py`

**Interfaces:**
- Produces:
  - `CallSummary` grown to: `id: UUID`, `tenant_id: UUID`, `status: str`, `room_name: str`, `patient_name: str | None`, `started_at: datetime | None`, `created_at: datetime`.
  - `class StartCallRequest(BaseModel)` — `form_id: UUID`.
  - `class JoinTokenResponse(BaseModel)` — `token: str`, `url: str`, `room_name: str`.

- [ ] **Step 1: Write the failing test** (`test_call_dtos.py`):

```python
from datetime import datetime, timezone
from uuid import uuid4

from vera_core.schemas import CallSummary, JoinTokenResponse, StartCallRequest


def test_call_summary_grown_fields() -> None:
    s = CallSummary(
        id=uuid4(), tenant_id=uuid4(), status="active", room_name="call--t--c",
        patient_name="Jane Doe", started_at=None, created_at=datetime.now(timezone.utc),
    )
    assert s.status == "active"
    assert s.room_name == "call--t--c"


def test_start_call_and_join_token_dtos() -> None:
    fid = uuid4()
    assert StartCallRequest(form_id=fid).form_id == fid
    jt = JoinTokenResponse(token="jwt", url="ws://x", room_name="call--t--c")
    assert jt.url == "ws://x"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest packages/vera_core/tests/unit/schemas/test_call_dtos.py -v`
Expected: FAIL — `ImportError` (`StartCallRequest`/`JoinTokenResponse`) and/or missing `CallSummary` fields.

- [ ] **Step 3: Edit `dto.py`** — replace the placeholder `CallSummary` and add the two DTOs:

```python
class CallSummary(BaseModel):
    """Verification-call list/summary row for Live Monitoring."""

    id: UUID
    tenant_id: UUID
    status: str
    room_name: str
    patient_name: str | None = None
    started_at: datetime | None = None
    created_at: datetime


class StartCallRequest(BaseModel):
    form_id: UUID


class JoinTokenResponse(BaseModel):
    token: str
    url: str
    room_name: str
```

Ensure `datetime` is imported and all three names are re-exported from `vera_core/schemas/__init__.py`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest packages/vera_core/tests/unit/schemas/test_call_dtos.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/schemas/dto.py packages/vera_core/src/vera_core/schemas/__init__.py packages/vera_core/tests/unit/schemas/test_call_dtos.py
git commit -m "feat(schemas): grow CallSummary + add StartCallRequest/JoinTokenResponse"
```

### Task 11: Wire `livekit` + `secrets` into `app.state`

**Files:**
- Modify: `apps/control_plane/src/control_plane/main.py`
- Modify: `apps/control_plane/src/control_plane/deps.py`

**Interfaces:**
- Consumes: `build_livekit_gateway` (Task 9), `EnvSecretProvider`.
- Produces: `create_app(..., livekit=None, secrets=None)` kwargs; `app.state.livekit`, `app.state.secrets`; `get_livekit(request) -> LiveKitGateway` dep + `LiveKit = Annotated[LiveKitGateway, Depends(get_livekit)]` alias in `common.py`.

- [ ] **Step 1: Add the deps accessor** — in `deps.py`, mirror the `get_kms` pattern:

```python
def get_livekit(request: Request) -> "LiveKitGateway":
    return request.app.state.livekit
```

(import `LiveKitGateway` under `TYPE_CHECKING` to avoid a cycle.)

- [ ] **Step 2: Add the DI alias** — in `api/v1/common.py`:

```python
LiveKit = Annotated["LiveKitGateway", Depends(get_livekit)]
```

- [ ] **Step 3: Wire into `create_app`/lifespan** — in `main.py`: add `livekit=None, secrets=None` params to `create_app`; inside `lifespan`, after the `kms` line:

```python
        app.state.secrets = secrets or EnvSecretProvider()
        app.state.livekit = livekit or build_livekit_gateway(settings, app.state.secrets)
```

- [ ] **Step 4: Verify control-plane app still boots (existing tests green)**

Run: `uv run pytest tests/integration/control_plane/test_admin.py -v`
Expected: PASS — but note the `authz_app` fixture will now need a fake `livekit`/`secrets` injected (handled in Task 12's fixture). If `build_livekit_gateway` raises because `VERA_LIVEKIT_URL` is unset under the existing fixture, that confirms the injection point; proceed to Task 12 which injects a fake.

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/main.py apps/control_plane/src/control_plane/deps.py apps/control_plane/src/control_plane/api/v1/common.py
git commit -m "feat(control-plane): wire LiveKit gateway + secret provider into app.state"
```

### Task 12: The three call endpoints

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py`
- Create: `tests/integration/control_plane/test_calls.py`
- Modify: `tests/integration/control_plane/conftest.py` (inject a fake `livekit` into `authz_app`)

**Interfaces:**
- Consumes: `LiveKit` dep, `TenantSession`, `TenantId`, `require`, `ok`/`ResponseModel`, `Call`/`CallEvent`/`PatientForm` models, `CallStatus`/`CallEventType`/`FormStatus` enums, `room_name_for_call`, DTOs from Task 10.
- Produces: `POST /calls` → `ResponseModel[CallSummary]`; `GET /calls/{call_id}/join-token` → `ResponseModel[JoinTokenResponse]`; `GET /calls` → `ResponseModel[list[CallSummary]]`.

> **Auth note (acknowledged stopgap):** all three guard with `require("calls:read")` for now (the SPA has no real auth yet; the spec flags this). A later task tightens `POST`/join-token to a write/manage permission once the RBAC catalog is extended.

- [ ] **Step 1: Inject a fake LiveKit gateway into the test app** — in `conftest.py`, add a fixture and pass it to `create_app` in `authz_app`:

```python
class FakeLiveKit:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.url = "ws://fake:7880"

    async def create_call_room(self, room_name: str) -> None:
        self.created.append(room_name)

    def mint_join_token(self, room_name: str, identity: str) -> str:
        return f"faketoken:{room_name}:{identity}"


@pytest.fixture(scope="session")
def fake_livekit() -> FakeLiveKit:
    return FakeLiveKit()
```

Add `fake_livekit` to `authz_app`'s params and pass `livekit=fake_livekit, secrets=EnvSecretProvider()` into `create_app(...)`.

- [ ] **Step 2: Write the failing tests** (`test_calls.py`) — mirror `test_admin.py` helpers (`_auth`, `_idem`); seed a `PatientForm` via `admin_sessionmaker`:

```python
import httpx
import pytest
from sqlalchemy import select

from vera_core.models import Call
from vera_core.observability.correlation import parse_room_name


@pytest.mark.asyncio
async def test_list_calls_empty_then_populated(
    client: httpx.AsyncClient, rbac_world, seeded_form_id
) -> None:
    # create a call
    resp = await client.post(
        "/api/v1/calls",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
        json={"form_id": str(seeded_form_id)},
    )
    assert resp.status_code == 200, resp.text
    summary = resp.json()["data"]
    assert summary["status"] == "initiated"
    assert parse_room_name(summary["room_name"]) is not None

    # it now appears in the list
    lst = await client.get(
        "/api/v1/calls",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
    )
    assert lst.status_code == 200, lst.text
    assert any(c["id"] == summary["id"] for c in lst.json()["data"])


@pytest.mark.asyncio
async def test_join_token_returns_room_scoped_token(
    client: httpx.AsyncClient, rbac_world, seeded_form_id
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
        json={"form_id": str(seeded_form_id)},
    )
    call_id = created.json()["data"]["id"]
    room = created.json()["data"]["room_name"]

    tok = await client.get(
        f"/api/v1/calls/{call_id}/join-token",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
    )
    assert tok.status_code == 200, tok.text
    body = tok.json()["data"]
    assert body["room_name"] == room
    assert body["token"].startswith("faketoken:")
```

Add a `seeded_form_id` fixture that inserts a `PatientForm` (needs a `schema_version_id` — reuse whatever the existing tests use to seed schema versions, or insert a minimal `SchemaVersion` row via `admin_sessionmaker`; check how `rbac_world`/other tests seed prerequisite rows).

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_calls.py -v`
Expected: FAIL — endpoints return `ok([])` / 404 for the new routes.

- [ ] **Step 4: Implement the endpoints in `calls.py`**

```python
from uuid import UUID

from fastapi import APIRouter

from control_plane.api.v1.common import LiveKit, TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode, NotFoundError
from control_plane.responses import ResponseModel, ok
from sqlalchemy import select
from vera_core.models import Call, CallEvent, PatientForm
from vera_core.models.enums import CallEventType, CallStatus, FormStatus
from vera_core.observability.correlation import room_name_for_call
from vera_core.schemas import CallSummary, JoinTokenResponse, StartCallRequest

router = APIRouter(tags=["calls"])

_ACTIVE_STATUSES = (
    CallStatus.INITIATED, CallStatus.RINGING, CallStatus.IVR,
    CallStatus.ACTIVE, CallStatus.WAITING, CallStatus.CRITICAL,
)


def _summary(call: Call, patient_name: str | None) -> CallSummary:
    return CallSummary(
        id=call.id, tenant_id=call.tenant_id, status=call.current_status,
        room_name=room_name_for_call(call.tenant_id, call.id),
        patient_name=patient_name, started_at=call.started_at,
        created_at=call.created_at,
    )


@router.post(
    "/calls",
    response_model=ResponseModel[CallSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def start_call(
    body: StartCallRequest,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    _caller: VerifiedIdentity = require("calls:read"),  # TODO: calls:write once catalog grows
) -> ResponseModel[CallSummary]:
    form = (
        await session.execute(select(PatientForm).where(PatientForm.id == body.form_id))
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError("patient form not found")

    call = Call(tenant_id=tenant_id, form_id=form.id, current_status=CallStatus.INITIATED)
    session.add(call)
    await session.flush()  # populates call.id (UUIDv7)

    room_name = room_name_for_call(tenant_id, call.id)
    await livekit.create_call_room(room_name)
    form.status = FormStatus.IN_QUEUE
    session.add(
        CallEvent(
            tenant_id=tenant_id, call_id=call.id,
            event_type=CallEventType.STATUS, event_value=CallStatus.INITIATED,
        )
    )
    return ok(_summary(call, form.patient_name))


@router.get(
    "/calls/{call_id}/join-token",
    response_model=ResponseModel[JoinTokenResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def join_token(
    call_id: UUID,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[JoinTokenResponse]:
    call = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()  # RLS already constrains to the caller's tenant
    if call is None:
        raise NotFoundError("call not found")
    room_name = room_name_for_call(tenant_id, call.id)
    identity = f"supervisor-{caller.user_id}"
    token = livekit.mint_join_token(room_name=room_name, identity=identity)
    return ok(JoinTokenResponse(token=token, url=livekit.url, room_name=room_name))


@router.get(
    "/calls",
    response_model=ResponseModel[list[CallSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def list_calls(
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[list[CallSummary]]:
    rows = (
        await session.execute(
            select(Call, PatientForm.patient_name)
            .join(PatientForm, PatientForm.id == Call.form_id)
            .where(Call.current_status.in_([s.value for s in _ACTIVE_STATUSES]))
            .order_by(Call.created_at.desc())
        )
    ).all()
    return ok([_summary(c, name) for c, name in rows])
```

> Confirm `NotFoundError` + its `DefaultExceptionCode` exist in `control_plane.exceptions`; if the codebase uses a different not-found helper, use that. Confirm `VerifiedIdentity` exposes `user_id` (else use the correct attribute for the caller's id).

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/integration/control_plane/test_calls.py -v`
Expected: PASS (requires `just up` for Postgres).

- [ ] **Step 6: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/calls.py tests/integration/control_plane/test_calls.py tests/integration/control_plane/conftest.py
git commit -m "feat(control-plane): real calls API (create+dispatch, join-token, active list)"
```

### Task 13: Wire observability into the control-plane lifespan

**Files:**
- Modify: `apps/control_plane/src/control_plane/main.py`

**Interfaces:**
- Consumes: `configure_observability` (`vera_core.observability.otel`).

- [ ] **Step 1: Call it in the lifespan** — replace the `# TODO(vera-2.x): observability …` line (main.py:84) with:

```python
        configure_observability(settings)
```

and add the import `from vera_core.observability.otel import configure_observability`.

- [ ] **Step 2: Verify boot + existing tests still green** (no-ops without `VERA_LANGFUSE_HOST`)

Run: `uv run pytest tests/integration/control_plane/test_admin.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add apps/control_plane/src/control_plane/main.py
git commit -m "feat(control-plane): initialize observability in lifespan (no-op without Langfuse host)"
```

### Task 14: Backend gate

- [ ] **Step 1: Run the full gate**

Run: `just check`
Expected: ruff clean, mypy clean, all pytest green (with `just up` running).

- [ ] **Step 2: Run `/simplify` on the backend changes** (per root CLAUDE.md), then re-run `just check`. Commit any cleanups:

```bash
git commit -am "refactor: simplify voice-pipeline backend per /simplify"
```

---

# Phase 5 — Frontend (`vera-frontend`, separate repo)

> All paths below are relative to `/Users/tapusd/Work/Techjays/Vera/vera-frontend`. Run commands from that directory.

### Task 15: LiveKit deps + API client

**Files:**
- Modify: `package.json`
- Create: `src/lib/api/client.ts`
- Create: `src/lib/api/calls.ts`
- Test: `src/lib/api/calls.test.ts`

**Interfaces:**
- Produces:
  - `apiFetch<T>(path: string, init?: RequestInit): Promise<T>` — prefixes `VITE_API_URL` (default `http://localhost:8000/api/v1`), attaches a dev bearer from `VITE_DEV_TOKEN`, unwraps `{ data }`.
  - `startCall(formId: string): Promise<CallSummary>`; `getJoinToken(callId: string): Promise<JoinTokenResponse>`; `listActiveCalls(): Promise<CallSummary[]>`.
  - TS types `CallSummary`, `JoinTokenResponse` mirroring the backend DTOs.

- [ ] **Step 1: Install deps**

Run: `npm install livekit-client @livekit/components-react @livekit/components-styles`
Expected: added to `package.json`.

- [ ] **Step 2: Write the failing test** (`src/lib/api/calls.test.ts`):

```ts
import { describe, expect, it, vi, beforeEach } from "vitest";
import { startCall, getJoinToken } from "./calls";

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

describe("calls api", () => {
  it("startCall posts form_id and unwraps data", async () => {
    (fetch as any).mockResolvedValue({
      ok: true,
      json: async () => ({ data: { id: "c1", room_name: "call--t--c1", status: "initiated" } }),
    });
    const summary = await startCall("f1");
    expect(summary.room_name).toBe("call--t--c1");
    const [, init] = (fetch as any).mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({ form_id: "f1" });
  });

  it("getJoinToken fetches token for a call", async () => {
    (fetch as any).mockResolvedValue({
      ok: true,
      json: async () => ({ data: { token: "jwt", url: "ws://x", room_name: "call--t--c1" } }),
    });
    const jt = await getJoinToken("c1");
    expect(jt.token).toBe("jwt");
    expect(jt.url).toBe("ws://x");
  });
});
```

- [ ] **Step 3: Run to verify it fails**

Run: `npm run test -- src/lib/api/calls.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 4: Implement `client.ts`**

```ts
const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000/api/v1";
const DEV_TOKEN = import.meta.env.VITE_DEV_TOKEN ?? "";

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(DEV_TOKEN ? { Authorization: `Bearer ${DEV_TOKEN}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) throw new Error(`API ${path} failed: ${res.status}`);
  const body = await res.json();
  return body.data as T;
}
```

- [ ] **Step 5: Implement `calls.ts`**

```ts
import { apiFetch } from "./client";

export type CallSummary = {
  id: string;
  tenant_id?: string;
  status: string;
  room_name: string;
  patient_name?: string | null;
  started_at?: string | null;
  created_at?: string;
};

export type JoinTokenResponse = { token: string; url: string; room_name: string };

export const startCall = (formId: string) =>
  apiFetch<CallSummary>("/calls", { method: "POST", body: JSON.stringify({ form_id: formId }) });

export const getJoinToken = (callId: string) =>
  apiFetch<JoinTokenResponse>(`/calls/${callId}/join-token`);

export const listActiveCalls = () => apiFetch<CallSummary[]>("/calls");
```

- [ ] **Step 6: Run to verify it passes**

Run: `npm run test -- src/lib/api/calls.test.ts`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add package.json package-lock.json src/lib/api/client.ts src/lib/api/calls.ts src/lib/api/calls.test.ts
git commit -m "feat: add LiveKit deps + calls API client"
```

### Task 16: IN QUEUE transition triggers `startCall`

**Files:**
- Modify: `src/lib/mock-data.ts` (extend `LiveCall` type)
- Modify: `src/pages/DataManagement.tsx` (the `handleStatusChange` handler at ~`DataManagement.tsx:126-129`)

**Interfaces:**
- Consumes: `startCall` (Task 15).
- Produces: when a patient transitions to `"IN QUEUE"`, fire `startCall(form.id)` and stash the returned `callId`/`roomName` (for now, log + optimistic local-state update; full Live Monitoring wiring is Task 17).

- [ ] **Step 1: Extend the `LiveCall` type** in `mock-data.ts` (add optional fields so real calls can carry identity):

```ts
  callId?: string;
  roomName?: string;
```

- [ ] **Step 2: Wire the handler** — in `DataManagement.tsx`, update `handleStatusChange` so that on a transition to `"IN QUEUE"` it calls the backend (keep the existing local-state update so the dummy UI still reflects the change):

```tsx
import { startCall } from "@/lib/api/calls";
// ...
const handleStatusChange = async (id: string, next: PatientFormStatus) => {
  setForms((prev) => prev.map((f) => (f.id === id ? { ...f, status: next } : f)));
  if (next === "IN QUEUE") {
    try {
      const call = await startCall(id);
      console.info("call started", call.id, call.room_name);
    } catch (e) {
      console.error("startCall failed", e);
    }
  }
};
```

- [ ] **Step 3: Type-check + lint**

Run: `npm run build` (runs `tsc -b`) — Expected: no type errors. `npm run lint` — Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add src/lib/mock-data.ts src/pages/DataManagement.tsx
git commit -m "feat: trigger backend call on IN QUEUE transition"
```

### Task 17: Live Monitoring list + Intervene → join token

**Files:**
- Modify: `src/pages/LiveMonitoring.tsx` (the Intervene handler at `LiveMonitoring.tsx:232-235`)

**Interfaces:**
- Consumes: `listActiveCalls`, `getJoinToken` (Task 15).
- Produces: the Intervene handler fetches a join token for the selected call and opens `InterveneModal` with `{ token, url, roomName }`; the active-call list is hydrated from `listActiveCalls()` (falling back to dummy data if the call fails, so the UI still renders offline).

- [ ] **Step 1: Hydrate the list** — add an effect that loads `listActiveCalls()` on mount and maps results onto the existing `LiveCall` shape (carry `callId`/`roomName`). Keep dummy data as the fallback when the fetch throws.

- [ ] **Step 2: Wire the Intervene handler** — replace the current `onIntervene={() => { setOverviewOpen(false); setInterveneOpen(true) }}` (LiveMonitoring.tsx:232-235) with a token fetch:

```tsx
onIntervene={async () => {
  if (!overviewCall?.callId) { setOverviewOpen(false); setInterveneOpen(true); return; }
  try {
    const jt = await getJoinToken(overviewCall.callId);
    setLiveKit({ token: jt.token, url: jt.url, roomName: jt.room_name });
  } catch (e) {
    console.error("getJoinToken failed", e);
  }
  setOverviewOpen(false);
  setInterveneOpen(true);
}}
```

Add `const [liveKit, setLiveKit] = useState<{token:string;url:string;roomName:string}|null>(null)` and pass `liveKit` into `<InterveneModal ... liveKit={liveKit} />`.

- [ ] **Step 3: Type-check + lint**

Run: `npm run build && npm run lint`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add src/pages/LiveMonitoring.tsx
git commit -m "feat: Intervene fetches LiveKit join token and opens room modal"
```

### Task 18: Render the LiveKit room in `InterveneModal`

**Files:**
- Modify: `src/components/monitoring/InterveneModal.tsx`
- Modify: `src/main.tsx` or the modal — import `@livekit/components-styles`.

**Interfaces:**
- Consumes: `liveKit: { token, url, roomName } | null` prop (Task 17); `LiveKitRoom`, `RoomAudioRenderer`, `useTracks`/transcription hook from `@livekit/components-react`.
- Produces: when `liveKit` is set, the modal hosts a connected room — agent audio plays, the Audio button toggles supervisor mic publish, and the Live Transcripts tab renders transcription segments. The "Connecting to call…" placeholder (InterveneModal.tsx:137-140) is replaced.

- [ ] **Step 1: Import the components stylesheet once** — in `src/main.tsx` add `import "@livekit/components-styles";`.

- [ ] **Step 2: Wrap the modal body in `LiveKitRoom`** — when `liveKit` is non-null, render:

```tsx
import { LiveKitRoom, RoomAudioRenderer, useLocalParticipant } from "@livekit/components-react";
// inside the modal body, replacing the "Connecting to call…" placeholder:
{liveKit ? (
  <LiveKitRoom serverUrl={liveKit.url} token={liveKit.token} connect audio>
    <RoomAudioRenderer />
    <LiveTranscript />
    <MicToggle />
  </LiveKitRoom>
) : (
  <div className="…">Connecting to call…</div>
)}
```

- [ ] **Step 3: Implement `MicToggle`** wired to the existing Audio button (InterveneModal.tsx:152-158) — toggles `localParticipant.setMicrophoneEnabled(...)` (the supervisor plays the payer rep):

```tsx
function MicToggle() {
  const { localParticipant } = useLocalParticipant();
  const [on, setOn] = useState(false);
  return (
    <button onClick={() => { localParticipant.setMicrophoneEnabled(!on); setOn(!on); }}>
      {on ? "Mute" : "Speak"}
    </button>
  );
}
```

- [ ] **Step 4: Implement `LiveTranscript`** — render the live transcript in the Live Transcripts tab.

> **Confirm the transcription API for the installed `@livekit/components-react` version** (it varies): recent versions expose `useTranscriptions()` returning segments with `{ text, participantInfo, final }`; older ones use `RoomEvent.TranscriptionReceived` or the `lk.transcription` text stream. Use `context7` (`mcp__plugin_context7_context7`) to fetch the current `@livekit/components-react` transcription docs, then implement accordingly. Minimal target:

```tsx
import { useTranscriptions } from "@livekit/components-react";
function LiveTranscript() {
  const segments = useTranscriptions();
  return (
    <div className="space-y-2">
      {segments.map((s, i) => (
        <p key={i}><b>{s.participantInfo?.identity ?? "agent"}:</b> {s.text}</p>
      ))}
    </div>
  );
}
```

- [ ] **Step 5: Type-check + lint**

Run: `npm run build && npm run lint`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/components/monitoring/InterveneModal.tsx src/main.tsx
git commit -m "feat: render LiveKit room (audio + mic + live transcript) in InterveneModal"
```

---

# Phase 6 — End-to-end validation & docs

### Task 19: Manual end-to-end run + devops-todo rows

**Files:**
- Modify: `adr/devops-todo.md`
- Create: `.env` entries (local only, not committed) — document required keys.

- [ ] **Step 1: Add provisioning rows to `adr/devops-todo.md`** — one row each for: LiveKit (prod server + `LIVEKIT_URL/API_KEY/API_SECRET` secret storage), Deepgram API key, Cartesia API key, Vertex/Gemini credentials, and a note that **the pipeline carries no PHI until the codec lands (synthetic data only)**.

- [ ] **Step 2: Populate local secrets** (uncommitted `.env` in repo root): `LIVEKIT_URL=ws://localhost:7880`, `VERA_LIVEKIT_URL=ws://localhost:7880`, `LIVEKIT_API_KEY=devkey`, `LIVEKIT_API_SECRET=secret`, `DEEPGRAM_API_KEY=…`, `CARTESIA_API_KEY=…`, and Google/Gemini creds (`GOOGLE_APPLICATION_CREDENTIALS` or `GOOGLE_API_KEY` per the `google` plugin). Mint a dev bearer (`just seed` / the test `_mint` path) and put it in the frontend `.env` as `VITE_DEV_TOKEN` + `VITE_API_URL=http://localhost:8000/api/v1`.

- [ ] **Step 3: Start everything**

Run (separate shells): `just up` ; `just migrate` ; `just api` ; `just worker` ; (in vera-frontend) `npm run dev`.
Expected: worker logs "registered worker" as `vera-agent`; control plane healthy at `:8000/healthz`; frontend at `:5173`.

- [ ] **Step 4: Walk the flow in the browser**

1. Data Management → open a `READY FOR PROCESSING` patient → set status to **IN QUEUE**. Confirm the worker log shows a job and `POST /api/v1/calls` 200.
2. Live Monitoring → the call appears → **Intervene**.
3. In `InterveneModal`: you hear the agent speak the greeting; click **Speak** and talk as the payer rep; confirm the agent responds and the Live Transcripts tab fills in.

Expected: a coherent spoken exchange; perceived turn latency in the ~1.0–1.6s band (US-hosted providers).

- [ ] **Step 5: Commit the docs**

```bash
git add adr/devops-todo.md
git commit -m "docs(devops): add LiveKit/Deepgram/Cartesia/Gemini provisioning rows + synthetic-data guardrail"
```

---

## Self-Review (completed during authoring)

- **Spec coverage:** worker cascade (Tasks 4–8) ✓; inert PHI seam (Tasks 2–3) ✓; control-plane create/dispatch + join-token + list (Tasks 9–13) ✓; frontend LiveKit integration (Tasks 15–18) ✓; LiveKit in compose (Task 1) ✓; correlation reuse (Tasks 8, 12) ✓; observability wiring (Task 13) ✓; testing strategy (fakes in CI, manual UI) ✓; devops-todo + synthetic-data guardrail (Task 19) ✓. Deferred items (tokenization, encryption, Redis stream, Postgres transcript persistence, SIP, supervisor, recording, extraction) are intentionally absent.
- **Placeholder scan:** the remaining "confirm against installed version" notes are deliberate verification steps for vendor APIs (livekit-agents node signatures, `livekit-api` builders, `@livekit/components-react` transcription) — each has a concrete reference implementation and a command to confirm, not an empty TODO.
- **Type consistency:** `room_name` / `JoinTokenResponse` / `CallSummary` field names match across DTOs (Task 10), endpoints (Task 12), and the frontend types (Task 15); `PHIBoundaryProtocol` method names match between Task 2, the seams (Task 5), and the factory (Task 3); `LiveKitGateway.create_call_room`/`mint_join_token`/`url` match between Task 9, the fake (Task 12), and the endpoints (Task 12).
- **Known open items (flagged, not gaps):** SPA auth is a dev-token stopgap (Task 15/19); `calls:read` guards the write endpoint until the RBAC catalog grows (Task 12); several vendor-API signatures require a one-line confirmation at implementation time.
