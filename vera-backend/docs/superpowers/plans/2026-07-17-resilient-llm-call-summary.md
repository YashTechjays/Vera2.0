# Resilient LLM Utility + Live-Call Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reusable fault-tolerant LLM utility (`vera_core.llm.ResilientLLM`, Gemini 3.1 Flash-Lite primary → GPT 5.4 mini fallback via LiveKit's `FallbackAdapter`) and a `GET /api/v1/calls/{call_id}/summary` endpoint that returns a 5s-cached supervisor-handoff summary of an ongoing call's diarized transcript.

**Architecture:** `vera_core/llm.py` wraps `livekit.agents.llm.FallbackAdapter` behind `LLMSpec` selectors + a provider registry — LiveKit types never leak to callers. `control_plane/call_summary.py` holds the transcript snapshot (Redis call-stream first, DB `Transcript` rows after finalization), diarized formatting, Redis cache, and orchestration. The endpoint mirrors `stream_call_events`' authz/audit chain exactly.

**Tech Stack:** Python 3.12, FastAPI, livekit-agents 1.5.17 (`google` + `openai` plugin extras), redis.asyncio, SQLAlchemy async, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-07-17-resilient-llm-call-summary-design.md`

## Global Constraints

- Working dir for all commands: `vera-backend/` repo root. Full gate: `just check` (ruff + mypy --strict + pytest).
- PHI: never log/trace prompt, transcript, summary text, or exception reprs — `type(exc).__name__` and counts only. No PHI in URLs.
- Async: `asyncio` only; never import `anyio`.
- Style: PEP 695 type params; ruff enforces. `from __future__ import annotations` where the codebase file does.
- Endpoints: `ResponseModel[T]` via `ok(...)`, errors via `CustomAPIException` (never `HTTPException`), `Cache-Control: no-store` on PHI responses, audit via shared helpers (never hand-rolled `AuditRecord` at a new call site).
- Never use `livekit.agents.inference.*`.
- Commits: conventional style, no Co-Authored-By lines.
- `livekit-agents>=1.5.17` floor (matches agent-worker).
- Integration tests need `just up` + migrated local Postgres/Redis; they auto-skip without it. Unit tests always run.

---

### Task 1: `vera_core.llm` — LLMSpec, provider registry, ResilientLLM

**Files:**
- Modify: `packages/vera_core/pyproject.toml` (add dependency)
- Create: `packages/vera_core/src/vera_core/llm.py`
- Test: `packages/vera_core/tests/unit/test_llm.py`

**Interfaces:**
- Consumes: `vera_core.config.secrets.SecretProvider` (protocol with `get(name) -> str`).
- Produces (used by Tasks 2–3):
  - `LLMSpec(provider: str, model: str, extra: Mapping[str, Any] = {})`, `LLMSpec.parse("google:gemini-3.1-flash-lite") -> LLMSpec`
  - `FallbackOptions(attempt_timeout: float = 8.0, max_retry_per_llm: int = 1, retry_interval: float = 0.5)`
  - `LLMUnavailableError(Exception)`
  - `ResilientLLM(primary: LLMSpec, fallbacks: Sequence[LLMSpec] = (), *, options: FallbackOptions = FallbackOptions(), secrets: SecretProvider | None = None, registry: Mapping[str, ProviderFactory] | None = None)` with `async complete(*, system: str, user: str) -> str` and `async aclose() -> None`
  - `OPENAI_API_KEY_SECRET = "OPENAI_API_KEY"`

- [ ] **Step 1: Add the dependency**

In `packages/vera_core/pyproject.toml`, append to `dependencies` (after the `"tzdata>=2025.1",` line):

```toml
    # Fault-tolerant out-of-pipeline LLM calls (vera_core.llm.ResilientLLM) wrap
    # livekit-agents' FallbackAdapter; google = Vertex Gemini, openai = GPT fallback.
    "livekit-agents[google,openai]>=1.5.17",
```

Run: `uv lock && uv sync --all-packages`
Expected: lock resolves; `livekit-plugins-openai` appears in `uv.lock`.
Verify: `uv run --package vera-core python -c "from livekit.plugins import openai, google; print('ok')"` → `ok`

- [ ] **Step 2: Write the failing tests**

Create `packages/vera_core/tests/unit/test_llm.py`:

```python
"""Unit tests for vera_core.llm — spec parsing, registry validation, fallback semantics.

Stub LLMs subclass the real livekit base classes so FallbackAdapter drives them
exactly as it would a production plugin.
"""

import pytest
from livekit.agents import APIConnectionError
from livekit.agents.llm import LLM, ChatChunk, ChoiceDelta, LLMStream
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from vera_core.llm import FallbackOptions, LLMSpec, LLMUnavailableError, ResilientLLM


class _StubStream(LLMStream):
    def __init__(self, llm, *, chat_ctx, conn_options, text, error):
        self._text = text
        self._error = error
        # super().__init__ starts the _run task immediately — set fields first.
        super().__init__(llm, chat_ctx=chat_ctx, tools=[], conn_options=conn_options)

    async def _run(self) -> None:
        if self._error:
            raise APIConnectionError("stub failure")
        self._event_ch.send_nowait(
            ChatChunk(id="stub", delta=ChoiceDelta(role="assistant", content=self._text))
        )


class _StubLLM(LLM):
    def __init__(self, *, text: str = "", error: bool = False) -> None:
        super().__init__()
        self._text = text
        self._error = error
        self.calls = 0

    def chat(self, *, chat_ctx, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **kwargs):
        self.calls += 1
        return _StubStream(
            self, chat_ctx=chat_ctx, conn_options=conn_options, text=self._text, error=self._error
        )


def _registry_for(*llms: _StubLLM):
    """A registry whose provider keys stub0, stub1, ... return the given LLMs."""
    return {f"stub{i}": (lambda spec, secrets, _l=l: _l) for i, l in enumerate(llms)}


def _specs(n: int) -> list[LLMSpec]:
    return [LLMSpec(provider=f"stub{i}", model="m") for i in range(n)]


def test_parse_selector() -> None:
    spec = LLMSpec.parse("google:gemini-3.1-flash-lite")
    assert spec == LLMSpec(provider="google", model="gemini-3.1-flash-lite")


@pytest.mark.parametrize("bad", ["", "google", ":model", "google:"])
def test_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        LLMSpec.parse(bad)


def test_unknown_provider_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown LLM provider"):
        ResilientLLM(LLMSpec(provider="nope", model="m"), registry={})


@pytest.mark.asyncio
async def test_primary_success_never_touches_fallback() -> None:
    primary = _StubLLM(text="primary answer")
    fallback = _StubLLM(text="fallback answer")
    specs = _specs(2)
    client = ResilientLLM(specs[0], [specs[1]], registry=_registry_for(primary, fallback))
    try:
        assert await client.complete(system="s", user="u") == "primary answer"
    finally:
        await client.aclose()
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_primary_failure_falls_back() -> None:
    primary = _StubLLM(error=True)
    fallback = _StubLLM(text="fallback answer")
    specs = _specs(2)
    client = ResilientLLM(
        specs[0],
        [specs[1]],
        options=FallbackOptions(attempt_timeout=1.0, max_retry_per_llm=0, retry_interval=0.0),
        registry=_registry_for(primary, fallback),
    )
    try:
        assert await client.complete(system="s", user="u") == "fallback answer"
    finally:
        await client.aclose()
    assert primary.calls >= 1
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_all_providers_failing_raises_unavailable() -> None:
    specs = _specs(2)
    client = ResilientLLM(
        specs[0],
        [specs[1]],
        options=FallbackOptions(attempt_timeout=1.0, max_retry_per_llm=0, retry_interval=0.0),
        registry=_registry_for(_StubLLM(error=True), _StubLLM(error=True)),
    )
    try:
        with pytest.raises(LLMUnavailableError):
            await client.complete(system="s", user="u")
    finally:
        await client.aclose()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run --package vera-core pytest packages/vera_core/tests/unit/test_llm.py -v`
Expected: FAIL / collection error — `vera_core.llm` does not exist.

- [ ] **Step 4: Implement `packages/vera_core/src/vera_core/llm.py`**

```python
"""Fault-tolerant LLM invocation for out-of-pipeline calls (summaries, analytics,
extraction) — NOT the live voice cascade (that stays in the agent worker's
AgentSession config).

Wraps livekit-agents' FallbackAdapter: callers declare an ordered chain of
provider/model selectors and get plain strings back; on a provider error or
attempt timeout the adapter moves to the next model transparently. LiveKit types
never cross this module's boundary — every caller in the codebase MUST go through
ResilientLLM rather than instantiating provider SDK / plugin clients directly
(see this package's CLAUDE.md).

PHI: prompts and completions routinely carry PHI. Nothing in this module logs
prompt/response text or exception reprs — provider errors can embed request
payloads — only exception type names and provider/model labels.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vera_core.config.secrets import SecretProvider

if TYPE_CHECKING:
    from livekit.agents.llm import LLM

logger = logging.getLogger(__name__)

OPENAI_API_KEY_SECRET = "OPENAI_API_KEY"

type ProviderFactory = Callable[["LLMSpec", SecretProvider | None], "LLM"]


class LLMUnavailableError(Exception):
    """Every provider in the chain failed. Carries no prompt/response text."""


@dataclass(frozen=True)
class LLMSpec:
    """One provider/model selector, e.g. LLMSpec("google", "gemini-3.1-flash-lite")."""

    provider: str
    model: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, selector: str) -> LLMSpec:
        """Parse a "provider:model" selector (the settings/env representation)."""
        provider, sep, model = selector.partition(":")
        if not sep or not provider.strip() or not model.strip():
            raise ValueError(f"invalid LLM selector {selector!r}; expected 'provider:model'")
        return cls(provider=provider.strip(), model=model.strip())


@dataclass(frozen=True)
class FallbackOptions:
    """Per-attempt budget and retry pacing, passed through to FallbackAdapter."""

    attempt_timeout: float = 8.0
    max_retry_per_llm: int = 1
    retry_interval: float = 0.5


def _build_google(spec: LLMSpec, secrets: SecretProvider | None) -> LLM:
    # Vertex AI path (ADC / Workload Identity creds) — same in-boundary route as
    # the live pipeline's cascade LLM.
    from livekit.plugins import google

    return google.LLM(model=spec.model, vertexai=True, **dict(spec.extra))


def _build_openai(spec: LLMSpec, secrets: SecretProvider | None) -> LLM:
    # OpenAI API — in-boundary under the signed BAA (repo-root CLAUDE.md trust
    # boundary). Key comes from the SecretProvider, never read from env directly.
    from livekit.plugins import openai

    if secrets is None:
        raise ValueError("openai provider requires a SecretProvider for OPENAI_API_KEY")
    return openai.LLM(model=spec.model, api_key=secrets.get(OPENAI_API_KEY_SECRET), **dict(spec.extra))


PROVIDERS: Mapping[str, ProviderFactory] = {
    "google": _build_google,
    "openai": _build_openai,
}


class ResilientLLM:
    """Fault-tolerant completion client over an ordered provider chain.

    Providers are validated at construction; the underlying plugin clients and
    the FallbackAdapter are built lazily on first complete() — LiveKit LLM
    clients open aiohttp sessions that need a running event loop (same rule as
    LiveKitGateway). Call aclose() at shutdown.
    """

    def __init__(
        self,
        primary: LLMSpec,
        fallbacks: Sequence[LLMSpec] = (),
        *,
        options: FallbackOptions = FallbackOptions(),
        secrets: SecretProvider | None = None,
        registry: Mapping[str, ProviderFactory] | None = None,
    ) -> None:
        self._specs: list[LLMSpec] = [primary, *fallbacks]
        self._options = options
        self._secrets = secrets
        self._registry = PROVIDERS if registry is None else registry
        for spec in self._specs:
            if spec.provider not in self._registry:
                raise ValueError(f"unknown LLM provider {spec.provider!r}")
        self._llms: list[LLM] = []
        self._chain: Any = None

    def _adapter(self) -> Any:
        if self._chain is None:
            from livekit.agents.llm import FallbackAdapter

            self._llms = [self._registry[s.provider](s, self._secrets) for s in self._specs]
            self._chain = FallbackAdapter(
                self._llms,
                attempt_timeout=self._options.attempt_timeout,
                max_retry_per_llm=self._options.max_retry_per_llm,
                retry_interval=self._options.retry_interval,
            )
        return self._chain

    async def complete(self, *, system: str, user: str) -> str:
        """One-shot completion: system + user message in, completion text out.

        Raises LLMUnavailableError when the whole chain is exhausted.
        """
        from livekit.agents.llm import ChatContext

        chat_ctx = ChatContext.empty()
        chat_ctx.add_message(role="system", content=system)
        chat_ctx.add_message(role="user", content=user)
        try:
            response = await self._adapter().chat(chat_ctx=chat_ctx).collect()
        except Exception as exc:  # payloads may carry PHI — type name only
            logger.warning("all LLM providers failed: %s", type(exc).__name__)
            raise LLMUnavailableError from exc
        return response.text

    async def aclose(self) -> None:
        chain, llms = self._chain, self._llms
        self._chain, self._llms = None, []
        if chain is not None:
            await chain.aclose()
        for llm in llms:
            await llm.aclose()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --package vera-core pytest packages/vera_core/tests/unit/test_llm.py -v`
Expected: all PASS. If `_StubLLM`/`_StubStream` trip on a base-class detail (e.g. an
abstract property), inspect the installed base with
`uv run --package agent-worker python -c "import inspect; from livekit.agents.llm.llm import LLM; print(inspect.getsource(LLM.__init__))"`
and adjust the stubs (never the production module) to satisfy it.

- [ ] **Step 6: Lint + typecheck the new module**

Run: `uv run ruff check packages/vera_core/src/vera_core/llm.py packages/vera_core/tests/unit/test_llm.py && uv run mypy packages/vera_core/src/vera_core/llm.py`
Expected: clean. (`Any` for the adapter internals is fine; the public surface is fully typed.)

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/pyproject.toml uv.lock packages/vera_core/src/vera_core/llm.py packages/vera_core/tests/unit/test_llm.py
git commit -m "feat(vera-core): ResilientLLM fault-tolerant LLM utility over FallbackAdapter"
```

---

### Task 2: `control_plane/call_summary.py` — snapshot, diarization, cache, orchestration

**Files:**
- Create: `apps/control_plane/src/control_plane/call_summary.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (move `_SOURCE_TO_ROLE` + `_transcript_role` here; import back)
- Test: `tests/unit/control_plane/test_call_summary.py`

**Interfaces:**
- Consumes: `vera_core.call_stream.CallStreamService.read_all(room_name) -> list[CallStreamEvent]` (+ `TYPE_TRANSCRIPT`), `vera_core.models.Transcript` (`source`, `role`, `message`, `seq`), `vera_core.transcript.ROLE_DTMF` / `source_for_role`, `vera_core.db.rls.tenant_session`, `vera_core.observability.correlation.room_name_for_call`, and (as a structural type) anything with `async complete(*, system: str, user: str) -> str` (Task 1's `ResilientLLM`).
- Produces (used by Task 3):
  - `TranscriptTurn(source: str, role: str, text: str)` (frozen dataclass)
  - `transcript_role(row: Transcript) -> str` (moved from calls.py)
  - `format_diarized(turns: Sequence[TranscriptTurn]) -> str`
  - `snapshot_turns(stream: CallStreamService, sessionmaker, tenant_id: UUID, call_id: UUID) -> list[TranscriptTurn]`
  - `SummaryCache` protocol (`get(room_name) -> str | None`, `set(room_name, payload, ttl_seconds) -> None`), `RedisSummaryCache(redis)`, `summary_cache_key(room_name) -> str`
  - `CallSummaryResponse(status: Literal["ready","pending"], summary: str | None, generated_at: int, turn_count: int)`
  - `SummaryLLM` protocol, `summarize_call(*, llm, cache, stream, sessionmaker, tenant_id, call_id, ttl_seconds) -> CallSummaryResponse` (raises `LLMUnavailableError` through)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/control_plane/test_call_summary.py`:

```python
"""Unit tests for call_summary: diarized formatting, Redis-first snapshot,
cache orchestration. The DB-fallback branch of snapshot_turns needs live
Postgres and is covered by the endpoint integration tests."""

import pytest

from control_plane.call_summary import (
    CallSummaryResponse,
    TranscriptTurn,
    format_diarized,
    snapshot_turns,
    summarize_call,
)
from vera_core.call_stream import CallStreamEvent, CallStreamService, TYPE_CALL_STATUS, TYPE_TRANSCRIPT
from vera_core.observability.correlation import room_name_for_call
from vera_core.db import uuid7


class _FakeStreamStore:
    """read_all-only CallStreamStore; other protocol methods unused here."""

    def __init__(self, events: list[CallStreamEvent]) -> None:
        self._events = events

    async def read_all(self, room_name: str) -> list[CallStreamEvent]:
        return self._events

    async def publish(self, room_name, event): ...
    async def mark_ended(self, room_name): ...
    async def delete(self, room_name): ...
    async def exists(self, room_name) -> bool:
        return bool(self._events)

    def read(self, room_name, *, first_entry_deadline_s=None): ...


class _DictCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.set_ttls: list[int] = []

    async def get(self, room_name: str) -> str | None:
        return self.data.get(room_name)

    async def set(self, room_name: str, payload: str, ttl_seconds: int) -> None:
        self.data[room_name] = payload
        self.set_ttls.append(ttl_seconds)


class _BrokenCache:
    async def get(self, room_name: str) -> str | None:
        raise ConnectionError("redis down")

    async def set(self, room_name: str, payload: str, ttl_seconds: int) -> None:
        raise ConnectionError("redis down")


class _StubLLM:
    def __init__(self, text: str = "the summary") -> None:
        self.text = text
        self.calls = 0

    async def complete(self, *, system: str, user: str) -> str:
        self.calls += 1
        self.last_user = user
        return self.text


def _turn_event(source: str, role: str, text: str, ts: int = 1) -> CallStreamEvent:
    return CallStreamEvent(
        type=TYPE_TRANSCRIPT, data={"role": role, "source": source, "text": text}, ts=ts
    )


def test_format_diarized_labels_speakers() -> None:
    turns = [
        TranscriptTurn(source="bot", role="agent", text="Hello, calling about a claim."),
        TranscriptTurn(source="rep", role="user", text="Member ID please?"),
        TranscriptTurn(source="bot", role="dtmf", text="1234"),
        TranscriptTurn(source="supervisor", role="agent", text="Taking over."),
    ]
    assert format_diarized(turns) == (
        "Vera (agent): Hello, calling about a claim.\n"
        "Payer rep: Member ID please?\n"
        "Vera (agent) [keypad]: 1234\n"
        "Supervisor: Taking over."
    )


@pytest.mark.asyncio
async def test_snapshot_prefers_live_stream_and_filters_non_transcript() -> None:
    events = [
        _turn_event("bot", "agent", "hi"),
        CallStreamEvent(type=TYPE_CALL_STATUS, data={"status": "active"}, ts=2),
        _turn_event("rep", "user", "hello"),
    ]
    stream = CallStreamService(_FakeStreamStore(events))
    turns = await snapshot_turns(stream, None, uuid7(), uuid7())  # sessionmaker unused
    assert turns == [
        TranscriptTurn(source="bot", role="agent", text="hi"),
        TranscriptTurn(source="rep", role="user", text="hello"),
    ]


async def _summarize(stream_events, cache, llm, ttl: int = 5) -> CallSummaryResponse:
    tenant_id, call_id = uuid7(), uuid7()
    return await summarize_call(
        llm=llm,
        cache=cache,
        stream=CallStreamService(_FakeStreamStore(stream_events)),
        sessionmaker=None,  # Redis path only in unit tests
        tenant_id=tenant_id,
        call_id=call_id,
        ttl_seconds=ttl,
    )


@pytest.mark.asyncio
async def test_summarize_ready_and_cached() -> None:
    cache, llm = _DictCache(), _StubLLM()
    events = [_turn_event("bot", "agent", "hi"), _turn_event("rep", "user", "hello")]
    result = await _summarize(events, cache, llm)
    assert result.status == "ready"
    assert result.summary == "the summary"
    assert result.turn_count == 2
    assert llm.calls == 1
    assert cache.set_ttls == [5]
    assert "Payer rep: hello" in llm.last_user


@pytest.mark.asyncio
async def test_summarize_cache_hit_skips_llm() -> None:
    cache, llm = _DictCache(), _StubLLM()
    tenant_id, call_id = uuid7(), uuid7()
    cached = CallSummaryResponse(status="ready", summary="old", generated_at=1, turn_count=2)
    cache.data[room_name_for_call(tenant_id, call_id)] = cached.model_dump_json()
    result = await summarize_call(
        llm=llm,
        cache=cache,
        stream=CallStreamService(_FakeStreamStore([])),
        sessionmaker=None,
        tenant_id=tenant_id,
        call_id=call_id,
        ttl_seconds=5,
    )
    assert result == cached
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_summarize_pending_below_min_turns_no_llm_no_cache() -> None:
    cache, llm = _DictCache(), _StubLLM()
    result = await _summarize([_turn_event("bot", "agent", "hi")], cache, llm)
    assert result.status == "pending"
    assert result.summary is None
    assert llm.calls == 0
    assert cache.data == {}


@pytest.mark.asyncio
async def test_summarize_dtmf_not_counted_as_speech() -> None:
    cache, llm = _DictCache(), _StubLLM()
    events = [_turn_event("bot", "agent", "hi"), _turn_event("bot", "dtmf", "1")]
    result = await _summarize(events, cache, llm)
    assert result.status == "pending"
    assert result.turn_count == 2


@pytest.mark.asyncio
async def test_cache_failure_degrades_to_fresh_compute() -> None:
    llm = _StubLLM()
    events = [_turn_event("bot", "agent", "hi"), _turn_event("rep", "user", "hello")]
    result = await _summarize(events, _BrokenCache(), llm)
    assert result.status == "ready"
    assert llm.calls == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/control_plane/test_call_summary.py -v`
Expected: FAIL — `control_plane.call_summary` does not exist.

- [ ] **Step 3: Implement `apps/control_plane/src/control_plane/call_summary.py`**

```python
"""On-demand supervisor-handoff summary of a live call's transcript.

Snapshot the diarized transcript (Redis call-event stream while the call is
live; persisted Transcript rows once the finalizer has drained it), format it
with speaker labels, and run it through the fault-tolerant ResilientLLM chain.
Results cache in Redis for a few seconds so tab-flipping supervisors don't fan
out LLM calls; a cache outage degrades to computing fresh (type-name-only logs —
the payload is PHI).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from vera_core.call_stream import TYPE_TRANSCRIPT, CallStreamService
from vera_core.db.rls import tenant_session
from vera_core.models import Transcript
from vera_core.observability.correlation import room_name_for_call
from vera_core.transcript import ROLE_DTMF, TurnRole, source_for_role

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

# Fewer real speech turns than this and there is nothing to brief — the endpoint
# reports "pending" without spending an LLM call.
_MIN_SPEECH_TURNS = 2

SUMMARY_SYSTEM_PROMPT = """\
You are briefing a human supervisor who is about to take over or monitor a live
insurance-verification phone call mid-flight. From the diarized transcript,
write a handoff summary covering:
- who is on the call and its purpose;
- what has been established so far (facts confirmed, answers collected);
- anything unresolved, contentious, or in progress;
- the likely next step.
Be concise (under 150 words), factual, and neutral. Do not invent details that
are not in the transcript."""

# Transcript.source ("rep"/"bot") -> envelope role, used only when the row's own
# `role` is blank (older rows / a source the worker didn't stamp a role for).
_SOURCE_TO_ROLE = {"rep": "user", "bot": "agent"}

_SPEAKER_LABELS = {"rep": "Payer rep", "bot": "Vera (agent)", "supervisor": "Supervisor"}


def transcript_role(row: Transcript) -> str:
    return row.role or _SOURCE_TO_ROLE.get(row.source, row.source)


@dataclass(frozen=True)
class TranscriptTurn:
    """One diarized turn, normalized from either the live stream or a DB row."""

    source: str
    role: str
    text: str


def format_diarized(turns: Sequence[TranscriptTurn]) -> str:
    """Render speaker-labelled lines: `Vera (agent): ...` / `Payer rep: ...`."""
    lines: list[str] = []
    for turn in turns:
        label = _SPEAKER_LABELS.get(turn.source, turn.source)
        if turn.role == ROLE_DTMF:
            label = f"{label} [keypad]"
        lines.append(f"{label}: {turn.text}")
    return "\n".join(lines)


async def snapshot_turns(
    stream: CallStreamService,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    tenant_id: UUID,
    call_id: UUID,
) -> list[TranscriptTurn]:
    """Current transcript snapshot: the live Redis stream while it exists, the
    persisted Transcript rows once the finalizer has drained it (mirrors the
    redis-or-DB branch of `stream_call_events`)."""
    events = await stream.read_all(room_name_for_call(tenant_id, call_id))
    turns = [
        TranscriptTurn(
            source=e.data.get("source") or source_for_role(e.data["role"]),
            role=e.data["role"],
            text=e.data["text"],
        )
        for e in events
        if e.type == TYPE_TRANSCRIPT
    ]
    if turns or sessionmaker is None:
        return turns
    async with tenant_session(sessionmaker, tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(Transcript).where(Transcript.call_id == call_id).order_by(Transcript.seq)
                )
            )
            .scalars()
            .all()
        )
    return [
        TranscriptTurn(source=row.source, role=transcript_role(row), text=row.message)
        for row in rows
    ]


def summary_cache_key(room_name: str) -> str:
    return f"vera:summary:{room_name}"


class SummaryCache(Protocol):
    async def get(self, room_name: str) -> str | None: ...
    async def set(self, room_name: str, payload: str, ttl_seconds: int) -> None: ...


class RedisSummaryCache:
    """Short-TTL summary cache in the in-boundary Memorystore (PHI at rest is
    CMEK-covered there; the TTL self-clears the key)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self, room_name: str) -> str | None:
        value = await self._redis.get(summary_cache_key(room_name))
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def set(self, room_name: str, payload: str, ttl_seconds: int) -> None:
        await self._redis.set(summary_cache_key(room_name), payload, ex=ttl_seconds)


class SummaryLLM(Protocol):
    """Structural view of vera_core.llm.ResilientLLM (keeps this module and its
    tests decoupled from the concrete class)."""

    async def complete(self, *, system: str, user: str) -> str: ...


class CallSummaryResponse(BaseModel):
    status: Literal["ready", "pending"]
    summary: str | None
    generated_at: int  # epoch milliseconds
    turn_count: int


async def summarize_call(
    *,
    llm: SummaryLLM,
    cache: SummaryCache,
    stream: CallStreamService,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    tenant_id: UUID,
    call_id: UUID,
    ttl_seconds: int,
) -> CallSummaryResponse:
    """Cache-through summary of the call's transcript so far. Raises
    LLMUnavailableError (from the llm) when every provider fails."""
    room_name = room_name_for_call(tenant_id, call_id)
    cached = None
    try:
        cached = await cache.get(room_name)
    except Exception as exc:  # cache outage degrades to fresh compute
        logger.warning("summary cache get failed: %s", type(exc).__name__)
    if cached is not None:
        return CallSummaryResponse.model_validate_json(cached)

    turns = await snapshot_turns(stream, sessionmaker, tenant_id, call_id)
    generated_at = int(time.time() * 1000)
    speech_turns = [t for t in turns if t.role != ROLE_DTMF]
    if len(speech_turns) < _MIN_SPEECH_TURNS:
        return CallSummaryResponse(
            status="pending", summary=None, generated_at=generated_at, turn_count=len(turns)
        )

    summary = await llm.complete(system=SUMMARY_SYSTEM_PROMPT, user=format_diarized(turns))
    response = CallSummaryResponse(
        status="ready", summary=summary, generated_at=generated_at, turn_count=len(turns)
    )
    try:
        await cache.set(room_name, response.model_dump_json(), ttl_seconds)
    except Exception as exc:
        logger.warning("summary cache set failed: %s", type(exc).__name__)
    return response
```

Note: `source_for_role` expects a `TurnRole`; if mypy flags the `e.data["role"]`
argument, narrow it with `cast("TurnRole", e.data["role"])` (the stream producer
only writes valid roles).

- [ ] **Step 4: Point calls.py at the moved helper**

In `apps/control_plane/src/control_plane/api/v1/calls.py`:
- Delete the module-level `_SOURCE_TO_ROLE` dict and the `_transcript_role` function.
- Add `from control_plane.call_summary import transcript_role` to the imports.
- Replace the one use `_transcript_role(row)` (in the terminal-branch of `stream_call_events`) with `transcript_role(row)`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/control_plane/test_call_summary.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint + typecheck**

Run: `uv run ruff check apps/control_plane tests/unit/control_plane/test_call_summary.py && uv run mypy apps/control_plane`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add apps/control_plane/src/control_plane/call_summary.py apps/control_plane/src/control_plane/api/v1/calls.py tests/unit/control_plane/test_call_summary.py
git commit -m "feat(control-plane): call-summary snapshot, diarized formatting, cached orchestration"
```

---

### Task 3: Settings, wiring, and the `GET /calls/{call_id}/summary` endpoint

**Files:**
- Modify: `packages/vera_core/src/vera_core/config/settings.py`
- Modify: `apps/control_plane/src/control_plane/exceptions.py` (add 503 code)
- Modify: `apps/control_plane/src/control_plane/main.py` (create_app params + app.state)
- Modify: `apps/control_plane/src/control_plane/deps.py` (getters)
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (shared authz helper + route)
- Test: `tests/integration/control_plane/test_call_summary_endpoint.py`

**Interfaces:**
- Consumes: Task 1's `ResilientLLM`/`LLMSpec`/`FallbackOptions`/`LLMUnavailableError`; Task 2's `summarize_call`/`CallSummaryResponse`/`RedisSummaryCache`/`SummaryCache`.
- Produces: `GET /api/v1/calls/{call_id}/summary` → `ResponseModel[CallSummaryResponse]`; `create_app(..., summary_llm=..., summary_cache=...)` injection seams; `DefaultExceptionCode.SERVICE_UNAVAILABLE` (503).

- [ ] **Step 1: Settings fields**

In `packages/vera_core/src/vera_core/config/settings.py`, after the `livekit_agent_name` field, add:

```python
    # --- live-call summary (control plane) -----------------------------------
    # Fault-tolerant summarizer chain, "provider:model" selectors resolved by
    # vera_core.llm (google = Vertex Gemini; openai = GPT under the OpenAI BAA).
    summary_primary_model: str = "google:gemini-3.1-flash-lite"  # VERA_SUMMARY_PRIMARY_MODEL
    summary_fallback_models: list[str] = ["openai:gpt-5.4-mini"]  # VERA_SUMMARY_FALLBACK_MODELS
    summary_attempt_timeout_seconds: float = 8.0  # VERA_SUMMARY_ATTEMPT_TIMEOUT_SECONDS
    # Short cache so tab-flipping supervisors reuse one summary; staleness cap.
    summary_cache_ttl_seconds: int = 5  # VERA_SUMMARY_CACHE_TTL_SECONDS

    @field_validator("summary_fallback_models", mode="before")
    @classmethod
    def _split_fallback_models(cls, value: object) -> object:
        # Accept a comma-separated string (friendlier than JSON in .env).
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value
```

- [ ] **Step 2: 503 exception code**

In `apps/control_plane/src/control_plane/exceptions.py`, add to `DefaultExceptionCode` after `BAD_GATEWAY`:

```python
    SERVICE_UNAVAILABLE = (503, "Service temporarily unavailable.")
```

- [ ] **Step 3: create_app wiring**

In `apps/control_plane/src/control_plane/main.py`:
- Import: `from control_plane.call_summary import RedisSummaryCache, SummaryCache` and `from vera_core.llm import FallbackOptions, LLMSpec, ResilientLLM`.
- Add `create_app` keyword params (alongside `secrets`, `livekit`, ...): `summary_llm: ResilientLLM | None = None,` and `summary_cache: SummaryCache | None = None,`.
- In the lifespan, right after the `app.state.livekit = ...` block:

```python
        # Fault-tolerant summarizer chain. Construction is lazy inside
        # ResilientLLM (no provider client until first use), so this is safe
        # even when the OpenAI key is absent in an env that never summarizes.
        owns_summary_llm = summary_llm is None
        app.state.summary_llm = summary_llm or ResilientLLM(
            LLMSpec.parse(settings.summary_primary_model),
            [LLMSpec.parse(selector) for selector in settings.summary_fallback_models],
            options=FallbackOptions(attempt_timeout=settings.summary_attempt_timeout_seconds),
            secrets=app.state.secrets,
        )
        app.state.summary_cache = summary_cache or RedisSummaryCache(_redis())
```

- In the lifespan's shutdown path (where other resources are closed, before the redis/engine teardown):

```python
            if owns_summary_llm:
                await app.state.summary_llm.aclose()
```

(Match the surrounding teardown structure — if teardown lives in a `finally:` block, put it there.)

- [ ] **Step 4: Dependency getters**

In `apps/control_plane/src/control_plane/deps.py` (near `get_call_stream_service`):

```python
def get_summary_llm(request: Request) -> ResilientLLM:
    llm: ResilientLLM = request.app.state.summary_llm
    return llm


def get_summary_cache(request: Request) -> SummaryCache:
    cache: SummaryCache = request.app.state.summary_cache
    return cache
```

with imports `from vera_core.llm import ResilientLLM` and `from control_plane.call_summary import SummaryCache` (use a `TYPE_CHECKING` import + string annotation if a circular import appears, mirroring `LiveKitGateway`).

- [ ] **Step 5: Factor the shared read-authz preamble and add the route**

In `apps/control_plane/src/control_plane/api/v1/calls.py`, extract the identical
authenticate→authorize→fetch→audit sequence of `stream_call_events` into one helper,
placed above `stream_call_events`:

```python
async def _authorize_call_read(
    call_id: UUID,
    request: Request,
    identity: VerifiedIdentity,
    sessionmaker: async_sessionmaker[AsyncSession],
    resolver: PermissionResolver,
    audit: AuditSink,
    *,
    resource_type: str,
) -> Call:
    """Shared read gate for the live-monitoring surfaces (event stream, summary):
    tenant caller + calls:read + owner-or-published visibility, with the folded
    authz+PHI audit record both endpoints must emit. Raises the same 404/403
    shapes as stream_call_events."""
    if identity.account_type != "tenant" or identity.tenant_id is None:
        raise NotFoundError(message="call not found")
    tenant_id = identity.tenant_id
    async with tenant_session(sessionmaker, tenant_id) as session:
        user_id, permissions = await resolver.effective_permissions(
            session, tenant_id, identity.user_id
        )
        call = (
            await session.execute(select(Call).where(Call.id == call_id))
        ).scalar_one_or_none()  # RLS already constrains to the caller's tenant
    if call is None:
        raise NotFoundError(message="call not found")
    if _call_hidden_from(call, user_id):
        raise NotFoundError(message="call not found")  # don't reveal a private call
    allowed = "calls:read" in permissions
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            actor_label=identity.email or identity.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type=resource_type,
            resource_id=str(call_id),
            permission_key="calls:read",
            decision="allow" if allowed else "deny",
            request_id=current_request_id(request),
        )
    )
    if not allowed:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="missing permission calls:read"
        )
    return call
```

Rewrite the body of `stream_call_events` to call
`call = await _authorize_call_read(call_id, request, identity, sessionmaker, resolver, audit, resource_type="call_events")`
in place of its inline preamble (everything from the `account_type` check through the
`FORBIDDEN` raise — behavior identical). `AuditSink` import comes from `vera_core.audit`.

Then add the new route after `stream_call_events`:

```python
@router.get(
    "/calls/{call_id}/summary",
    response_model=ResponseModel[CallSummaryResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.SERVICE_UNAVAILABLE,
    ),
)
async def get_call_summary(
    call_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    audit: Audit,
    stream: Annotated[CallStreamService, Depends(get_call_stream_service)],
    summary_llm: Annotated[ResilientLLM, Depends(get_summary_llm)],
    summary_cache: Annotated[SummaryCache, Depends(get_summary_cache)],
    settings: AppSettings,
) -> ResponseModel[CallSummaryResponse]:
    """On-demand supervisor-handoff summary of the call's transcript so far
    (Live Monitoring's Summary tab). Same visibility/authz/audit gate as the
    event stream; result is cached a few seconds (settings.summary_cache_ttl_seconds)
    so repeated tab flips don't fan out LLM calls."""
    call = await _authorize_call_read(
        call_id, request, identity, sessionmaker, resolver, audit, resource_type="call_summary"
    )
    assert identity.tenant_id is not None  # _authorize_call_read guaranteed it
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await summarize_call(
            llm=summary_llm,
            cache=summary_cache,
            stream=stream,
            sessionmaker=sessionmaker,
            tenant_id=identity.tenant_id,
            call_id=call.id,
            ttl_seconds=settings.summary_cache_ttl_seconds,
        )
    except LLMUnavailableError as exc:
        raise CustomAPIException(
            DefaultExceptionCode.SERVICE_UNAVAILABLE,
            message="summary temporarily unavailable",
        ) from exc
    return ok(result)
```

New imports in calls.py: `AppSettings` (from `control_plane.api.v1.common`),
`get_summary_cache`, `get_summary_llm` (from `control_plane.deps`),
`CallSummaryResponse`, `SummaryCache`, `summarize_call`, `transcript_role`
(from `control_plane.call_summary`), `ResilientLLM`, `LLMUnavailableError`
(from `vera_core.llm`), `AuditSink` (from `vera_core.audit`).

- [ ] **Step 6: Write the integration tests**

Create `tests/integration/control_plane/test_call_summary_endpoint.py`:

```python
"""Integration tests for GET /calls/{call_id}/summary — live RLS Postgres,
in-memory call stream + injected stub summarizer (conftest authz_app; the
app.state seams are overridden per-test)."""

from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.integration.control_plane.conftest import RBACWorld, seed_call
from tests.integration.control_plane.test_calls import _auth, seeded_form_id  # noqa: F401  # fixture
from vera_core.db.rls import tenant_session
from vera_core.llm import LLMUnavailableError
from vera_core.models import AuditLog, Transcript
from vera_core.observability.correlation import room_name_for_call


class _StubSummaryLLM:
    def __init__(self, text: str = "handoff summary", *, fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls = 0

    async def complete(self, *, system: str, user: str) -> str:
        self.calls += 1
        if self.fail:
            raise LLMUnavailableError
        return self.text


class _DictCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, room_name: str) -> str | None:
        return self.data.get(room_name)

    async def set(self, room_name: str, payload: str, ttl_seconds: int) -> None:
        self.data[room_name] = payload


@pytest.fixture
def stub_llm(authz_app) -> AsyncGenerator[_StubSummaryLLM]:
    """Swap the app's summarizer seams for deterministic fakes, restore after."""
    llm, cache = _StubSummaryLLM(), _DictCache()
    prior_llm = authz_app.state.summary_llm
    prior_cache = authz_app.state.summary_cache
    authz_app.state.summary_llm = llm
    authz_app.state.summary_cache = cache
    yield llm
    authz_app.state.summary_llm = prior_llm
    authz_app.state.summary_cache = prior_cache


async def _publish_turns(call_stream_service, room_name: str) -> None:
    await call_stream_service.publish_turn(room_name, "agent", "Hello, verifying benefits.", ts=1)
    await call_stream_service.publish_turn(room_name, "user", "Sure, member ID please.", ts=2)


@pytest.mark.asyncio
async def test_summary_ready_cached_audited_no_store(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id,
    admin_sessionmaker: async_sessionmaker,
    call_stream_service,
    stub_llm: _StubSummaryLLM,
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="active",
    )
    room = room_name_for_call(rbac_world.tenant_id, call_id)
    await _publish_turns(call_stream_service, room)

    resp = await client.get(f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()["data"]
    assert body["status"] == "ready"
    assert body["summary"] == "handoff summary"
    assert body["turn_count"] == 2

    # Second hit within the cache TTL: served from cache, no second LLM call.
    resp2 = await client.get(f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.admin_token))
    assert resp2.status_code == 200
    assert stub_llm.calls == 1

    # PHI disclosure audited with the call_summary resource type.
    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.resource_type == "call_summary",
                        AuditLog.resource_id == str(call_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) >= 2
    assert all(r.decision == "allow" for r in rows)


@pytest.mark.asyncio
async def test_summary_pending_for_quiet_call(
    client, rbac_world, seeded_form_id, admin_sessionmaker, stub_llm
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="active",
    )
    resp = await client.get(f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "pending"
    assert stub_llm.calls == 0


@pytest.mark.asyncio
async def test_summary_terminal_call_uses_db_transcript(
    client, rbac_world, seeded_form_id, admin_sessionmaker, stub_llm
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="completed",
    )
    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as session:
        session.add(
            Transcript(
                tenant_id=rbac_world.tenant_id, call_id=call_id, seq=1,
                source="bot", role="agent", message="Hello, verifying benefits.",
            )
        )
        session.add(
            Transcript(
                tenant_id=rbac_world.tenant_id, call_id=call_id, seq=2,
                source="rep", role="user", message="Sure, member ID please.",
            )
        )
    resp = await client.get(f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "ready"
    assert stub_llm.calls == 1


@pytest.mark.asyncio
async def test_summary_authz_denied_and_hidden(
    client, rbac_world, seeded_form_id, admin_sessionmaker, stub_llm
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,  # private to admin
        status="active",
    )
    # No calls:read on an ownerless/published call -> 403; here the call is
    # PRIVATE to admin, so a non-owner (even with calls:read) gets 404.
    resp = await client.get(
        f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.listener_token)
    )
    assert resp.status_code == 404
    # norole caller on an unknown call id -> 404 as well.
    resp = await client.get(
        "/api/v1/calls/00000000-0000-0000-0000-000000000000/summary",
        headers=_auth(rbac_world.norole_token),
    )
    assert resp.status_code == 404
    assert stub_llm.calls == 0


@pytest.mark.asyncio
async def test_summary_403_when_visible_but_unpermitted(
    client, rbac_world, seeded_form_id, admin_sessionmaker, call_stream_service, stub_llm
) -> None:
    call_id = await seed_call(
        admin_sessionmaker, rbac_world.tenant_id, seeded_form_id, published=True, status="active"
    )
    resp = await client.get(
        f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.norole_token)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_summary_llm_unavailable_returns_503(
    client, rbac_world, seeded_form_id, admin_sessionmaker, call_stream_service, authz_app
) -> None:
    llm, cache = _StubSummaryLLM(fail=True), _DictCache()
    prior_llm, prior_cache = authz_app.state.summary_llm, authz_app.state.summary_cache
    authz_app.state.summary_llm, authz_app.state.summary_cache = llm, cache
    try:
        call_id = await seed_call(
            admin_sessionmaker,
            rbac_world.tenant_id,
            seeded_form_id,
            initiated_by_id=rbac_world.admin_id,
            status="active",
        )
        room = room_name_for_call(rbac_world.tenant_id, call_id)
        await _publish_turns(call_stream_service, room)
        resp = await client.get(
            f"/api/v1/calls/{call_id}/summary", headers=_auth(rbac_world.admin_token)
        )
        assert resp.status_code == 503
        assert resp.json()["error_code"] == "SERVICE_UNAVAILABLE"
    finally:
        authz_app.state.summary_llm, authz_app.state.summary_cache = prior_llm, prior_cache
```

Adjust fixture names/imports to what `tests/integration/control_plane/conftest.py`
actually exports (`client`, `authz_app`, `admin_sessionmaker`, `call_stream_service`,
`rbac_world` are session/function fixtures there; `seeded_form_id` and `_auth` live in
`test_calls.py` — if importing them cross-module is awkward, copy `_auth` (3 lines) and
move/duplicate the `seeded_form_id` fixture into the new module). Check the error
envelope field name (`error_code`) against `control_plane/responses.py::ErrorResponse`
and match it.

- [ ] **Step 7: Run the new tests**

Run: `uv run pytest tests/integration/control_plane/test_call_summary_endpoint.py -v`
Expected: all PASS with `just up` + `just migrate` done (they skip without Postgres).
Also run the neighbors to prove the refactor broke nothing:
`uv run pytest tests/integration/control_plane/test_calls.py -v` → all PASS.

- [ ] **Step 8: Run the full gate**

Run: `just check`
Expected: ruff, mypy --strict, and pytest all green.

- [ ] **Step 9: Commit**

```bash
git add packages/vera_core/src/vera_core/config/settings.py apps/control_plane/src/control_plane/exceptions.py apps/control_plane/src/control_plane/main.py apps/control_plane/src/control_plane/deps.py apps/control_plane/src/control_plane/api/v1/calls.py tests/integration/control_plane/test_call_summary_endpoint.py
git commit -m "feat(control-plane): GET /calls/{id}/summary — cached supervisor-handoff summary"
```

---

### Task 4: Documentation — trust boundary, ResilientLLM usage rule, secrets

**Files:**
- Modify: `CLAUDE.md` (vera-backend root — trust boundary)
- Modify: `packages/vera_core/src/vera_core/CLAUDE.md` (usage rule)
- Modify: `env.example`
- Modify: `adr/devops-todo.md`

- [ ] **Step 1: Root trust boundary** (`CLAUDE.md` at vera-backend root)

In the **Trust boundary** section, change the INSIDE list to include the OpenAI API:

- Old: `... Twilio (SIP), LiveKit (self-hosted OSS — never LiveKit Cloud), Vertex AI Gemini (LLM), self-hosted Langfuse on GKE.`
- New: `... Twilio (SIP), LiveKit (self-hosted OSS — never LiveKit Cloud), Vertex AI Gemini (LLM), OpenAI API (LLM — BAA signed 2026-07; fallback tier for out-of-pipeline calls via vera_core.llm), self-hosted Langfuse on GKE.`

In **Bright lines**, update the LLM line:

- Old: `- NEVER send PHI to an LLM outside the BAA boundary. The pipeline's only LLM is in-boundary Vertex AI Gemini, and raw transcript PHI reaching it is expected (no tokenization) — but never route a prompt or PHI to a non-BAA model or API.`
- New: `- NEVER send PHI to an LLM outside the BAA boundary. The BAA-covered LLMs are Vertex AI Gemini (the live pipeline's only LLM) and the OpenAI API (fallback tier for out-of-pipeline calls, via vera_core.llm only); raw transcript PHI reaching them is expected (no tokenization) — but never route a prompt or PHI to any other model or API.`

- [ ] **Step 2: vera_core scoped rule** (`packages/vera_core/src/vera_core/CLAUDE.md`)

Add a new section after "No PHI tokenization (the voice pipeline)":

```markdown
## Out-of-pipeline LLM calls go through `vera_core.llm.ResilientLLM` — always

Any LLM call outside the live voice cascade (summaries, analytics, extraction,
post-call processing) MUST be made through `vera_core.llm.ResilientLLM` with
`LLMSpec` provider/model selectors — never by instantiating a provider SDK or a
LiveKit plugin LLM client directly at a call site. ResilientLLM wraps
livekit-agents' FallbackAdapter (ordered provider chain, per-attempt timeout,
retries) and is the single place provider construction, secret resolution
(`OPENAI_API_KEY` via SecretProvider), and PHI-safe error logging live. Adding a
provider means one entry in `vera_core.llm.PROVIDERS`, nothing else. The live
cascade's LLM (the agent worker's AgentSession) is separate and stays in
`apps/agent_worker` — do not route it through ResilientLLM.
```

- [ ] **Step 3: env.example + devops-todo**

In `env.example`, add near the other secret entries (match the file's comment style):

```bash
# OpenAI API key — fallback LLM tier for out-of-pipeline calls (vera_core.llm).
# BAA-covered. Resolved via SecretProvider; Secret Manager in prod.
OPENAI_API_KEY=
```

In `adr/devops-todo.md`, add a row to the secrets table (match the existing row
format for DEEPGRAM_API_KEY/CARTESIA_API_KEY): `OPENAI_API_KEY` — control plane —
summary fallback LLM — Secret Manager + CMEK.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md packages/vera_core/src/vera_core/CLAUDE.md env.example adr/devops-todo.md
git commit -m "docs: OpenAI joins the BAA trust boundary; ResilientLLM usage rule"
```

---

### Task 5: Simplify + final gate

- [ ] **Step 1: Run the code-simplifier** on the branch's changes (repo-wide CLAUDE.md
mandate): launch the `code-simplifier` agent targeting the files modified in Tasks 1–4.
Behavior must not change; tests are the referee.

- [ ] **Step 2: Re-run the full gate**

Run: `just check`
Expected: green. If the simplifier touched anything, re-run the integration tests too:
`uv run pytest tests/integration/control_plane/ -v`

- [ ] **Step 3: Commit any refinements**

```bash
git add -A && git commit -m "refactor: simplifier pass over resilient-LLM summary feature"
```

(Skip the commit if the simplifier made no changes.)
