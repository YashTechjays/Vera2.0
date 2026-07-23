# Per-model "thinking" tuning (thinking_budget / thinking_level) for the voice cascade LLM

**Date:** 2026-07-23
**Status:** Approved for implementation
**Extends:** `2026-07-23-dynamic-voice-llm-model-config-design.md` (the runtime LLM
model override this builds on)
**Related:** `apps/agent_worker/src/agent_worker/cascade.py` (hardcodes
`ThinkingConfig(thinking_budget=0)` today, unconditionally)

## Problem

Once the LLM model became a runtime, freeform override, a real defect surfaced:
`cascade.py` always passes `ThinkingConfig(thinking_budget=0)` regardless of which
model is selected. Gemini 2.5 (and earlier) models honor `thinking_budget`; Gemini
3.x models do not — they use a different field, `thinking_level`
(`minimal`/`low`/`medium`/`high`), on the same `ThinkingConfig` object. Confirmed by
reading the installed `livekit-plugins-google` source
(`.venv/lib/python3.12/site-packages/livekit/plugins/google/llm.py:43-45,
347-389`):

- Model-family detection is a simple substring check:
  `def _is_gemini_3_model(model: str) -> bool: return "gemini-3" in model.lower()`.
- For a Gemini 3 model, passing `thinking_budget` (with no `thinking_level`) only
  **warns and ignores it** — the exact symptom reported ("Model gemini-3.6-flash is
  Gemini 3 which does not support thinking_budget... Ignoring thinking_budget").
- For a **pre-3** model, passing `thinking_level` (with no `thinking_budget`) makes
  the plugin **raise `ValueError`** — a hard crash, not a warning. Any admin-facing
  validation must prevent this combination before it ever reaches a live call.

Beyond fixing the warning, there's a second, independently-motivated ask: expose
`thinking_budget`/`thinking_level` as an admin-tunable setting (not just an internal
default), so the team can trial latency/quality tradeoffs across the voice pipeline
without a redeploy — the same value proposition as the model-override feature
itself — and make the *active* choice visible in traces for that analysis.

## Decision (user-confirmed)

1. **New nullable JSONB column, `extra_config`, on the existing `voice_model_config`
   table** (additive migration — existing rows get `NULL`, meaning "no override, use
   the per-family default"). Chosen over new columns per-knob because it generalizes
   to "any kind of custom configuration per provider or model" without a schema
   change — the same reasoning that already shaped the `stage`/`provider` columns.
2. **`thinking_budget` is a free-form integer input** in the UI (not a preset
   dropdown) — the point is dialing in an arbitrary value for live performance
   testing, not picking from a fixed list.
3. **`thinking_level` exposes the full Gemini API enum**: `minimal`, `low`,
   `medium`, `high` (not just the two — `low`/`high` — the plugin's warning message
   happens to recommend for a manual override).
4. **Backend AND frontend both validate the model-family / field pairing** — a
   `thinking_level` value requires a Gemini-3-family model name; a `thinking_budget`
   value requires a non-Gemini-3 model name. Rejected with 422 at save time. This is
   not just consistency — it prevents the pre-3 `ValueError` crash described above.
5. **The frontend's thinking control swaps live** based on the current model-name
   input (mirroring the same family heuristic in TypeScript): a `thinking_level`
   dropdown for a Gemini-3-looking name, a `thinking_budget` number field otherwise.
6. **Active model + thinking config are traced** as span attributes on the per-call
   root span, `vera.*`-prefixed (matching the existing `vera.room`/`vera.tenant_id`/
   `vera.call_id` convention in `call_trace_attributes`) — `vera.llm.model`,
   and exactly one of `vera.llm.thinking_budget` / `vera.llm.thinking_level`. Per-turn
   `gen_ai.request.model` is already auto-captured by livekit-agents' own
   instrumentation (confirmed: `livekit/agents/voice/generation.py`'s
   `_llm_inference_task` sets it from `self.llm.model`) — the new call-level
   attribute is additive, for filtering/aggregating by model across a whole call
   without drilling into per-turn spans.
7. **No admin override → an explicit, stable default per family** — `thinking_budget=0`
   for pre-3 (unchanged), `thinking_level="low"` for Gemini 3 (a public, documented
   value) rather than passing an empty `ThinkingConfig()` and relying on the
   plugin's own private `_is_gemini_3_flash_model`-driven auto-selection. This keeps
   the default deterministic, keeps tracing accurate in the no-override case (we
   always know exactly what we set), and avoids depending on underscore-prefixed
   plugin internals that could change without notice.
8. **Continuation of the same branch/PR** (PR #127, currently open, unreviewed) —
   not a new PR, since this is a direct extension of the same feature.

## Design

### 1. Data model — `voice_model_config.extra_config`

`packages/vera_core/src/vera_core/models/voice_model_config.py` gains one column:

```python
from sqlalchemy.dialects.postgresql import JSONB
...
    extra_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
```

No new CHECK constraint at the DB level — the single write path
(`save_llm_model`) is where `ThinkingOverride` validation happens; a JSONB shape
CHECK would add complexity disproportionate to the value here (the column has no
independent write path to guard against).

**Migration** (new — do NOT edit the already-applied Task-1 migration; several
local/CI databases have already run it, so an in-place edit would silently diverge
between environments): `ALTER TABLE voice_model_config ADD COLUMN IF NOT EXISTS
extra_config JSONB`, chained via `down_revision` onto the current head
(`e3e633747040`, Task 2's permission seed).

### 2. `ThinkingOverride` + family validation — `vera_core/services/model_config.py`

```python
from typing import Literal
from pydantic import BaseModel, model_validator

class ThinkingOverride(BaseModel):
    thinking_budget: int | None = None
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "ThinkingOverride":
        if (self.thinking_budget is None) == (self.thinking_level is None):
            raise ValueError("exactly one of thinking_budget or thinking_level must be set")
        return self


def is_gemini_3_model(model: str) -> bool:
    """Mirrors livekit-plugins-google's own detection (llm.py::_is_gemini_3_model) —
    keep this in lockstep with that heuristic; drift here would let an incompatible
    thinking_level/thinking_budget pairing reach the plugin, which raises ValueError
    for thinking_level on a pre-3 model."""
    return "gemini-3" in model.lower()


class InvalidThinkingOverride(ValueError):
    pass


def validate_extra_config(model: str, extra_config: ThinkingOverride | None) -> None:
    if extra_config is None:
        return
    is_gemini_3 = is_gemini_3_model(model)
    if extra_config.thinking_level is not None and not is_gemini_3:
        raise InvalidThinkingOverride(
            f"thinking_level requires a Gemini 3 model; {model!r} is not Gemini 3"
        )
    if extra_config.thinking_budget is not None and is_gemini_3:
        raise InvalidThinkingOverride(
            f"thinking_budget is not supported on Gemini 3 models ({model!r}) — "
            "use thinking_level instead"
        )
```

`save_llm_model` gains `extra_config: ThinkingOverride | None` (validated before
insert, stored as `extra_config.model_dump(exclude_none=True)` — always exactly one
key, `thinking_budget` or `thinking_level`, never both, matching the JSONB shape
`add_llm_model_override_metadata`/`cascade.py` expect). `reset_llm_model` is
unchanged — a reset row already has `provider=None, model=None`; `extra_config`
simply isn't set on that row (stays `NULL`), consistent with "reset clears
everything back to hardcoded defaults."

`add_llm_model_override_metadata` also threads the JSONB dict into dispatch
metadata when present: `metadata["llm_thinking_override"] = current.extra_config`,
right alongside the existing `metadata["llm_model_override"] = current.model`. Same
fail-safe wrapper (unchanged) — a broken read degrades to neither key being set.

### 3. control_plane endpoints — `api/v1/llm_config.py`

`SaveLlmConfigRequest` and `LlmConfigState` both gain
`extra_config: ThinkingOverride | None = None`. `_state()` maps the stored JSONB
dict back through `ThinkingOverride.model_validate(...)` when present.
`save_llm_config` catches the new `InvalidThinkingOverride` alongside the existing
`InvalidModelName`, both mapping to `DefaultExceptionCode.VALIDATION_ERROR` (422).

### 4. agent_worker — `cascade.py` + `main.py`

`cascade.py` gains two small pure functions and a new `build_session` parameter:

```python
from vera_core.services.model_config import is_gemini_3_model

def resolve_thinking_config(model: str, thinking_override: dict[str, Any] | None) -> ThinkingConfig:
    if thinking_override:
        return ThinkingConfig(**thinking_override)
    if is_gemini_3_model(model):
        return ThinkingConfig(thinking_level="low")
    return ThinkingConfig(thinking_budget=0)


def llm_trace_attributes(model: str, thinking_config: ThinkingConfig) -> dict[str, str | int]:
    attrs: dict[str, str | int] = {"vera.llm.model": model}
    if thinking_config.thinking_budget is not None:
        attrs["vera.llm.thinking_budget"] = thinking_config.thinking_budget
    if thinking_config.thinking_level is not None:
        attrs["vera.llm.thinking_level"] = str(thinking_config.thinking_level)
    return attrs
```

`build_session(..., thinking_override: dict[str, Any] | None = None)` calls
`resolve_thinking_config(resolve_llm_model(llm_model), thinking_override)` instead
of the hardcoded `ThinkingConfig(thinking_budget=0)`.

`main.py`'s `entrypoint()` reads the new metadata key and sets the trace attributes
on the same per-call root span `call_trace_attributes` already populates (confirmed
this is the root span, not a per-turn one: `job_proc_lazy_main.py` wraps the whole
`entrypoint()` in `@tracer.start_as_current_span("job_entrypoint")`, and nothing
between there and `build_session` opens a narrower span):

```python
resolved_model = resolve_llm_model(meta.get("llm_model_override"))
thinking_cfg = resolve_thinking_config(resolved_model, meta.get("llm_thinking_override"))
trace.get_current_span().set_attributes(llm_trace_attributes(resolved_model, thinking_cfg))

session = build_session(
    vad=ctx.proc.userdata.get("vad"),
    key_terms=controller.plan.stt_key_terms if controller is not None else None,
    llm_model=meta.get("llm_model_override"),
    thinking_override=meta.get("llm_thinking_override"),
)
```

Calling `resolve_llm_model`/`resolve_thinking_config` here duplicates the same
(cheap, pure, side-effect-free) computation `build_session` does internally —
accepted, since it avoids widening `build_session`'s return contract just to hand
back values used only for tracing.

### 5. Frontend

`src/lib/api/llmConfig.ts`: `LlmConfigState` gains
`extra_config: ThinkingOverride | null`; `saveLlmConfig(model, extraConfig)` gains
a second parameter. `src/pages/llmConfig.helpers.ts` gains
`isGemini3Model(model: string): boolean` (mirrors the backend substring check
exactly) and a `THINKING_LEVELS` constant. `src/pages/LlmConfig.tsx` gains two more
local inputs (thinking-budget text, thinking-level select), swapping which one
renders based on `isGemini3Model(input)` on the *current* model text — not just the
saved value — and clearing the now-irrelevant field on a family switch so a stale
value from before can't be silently submitted. History table gains a "Thinking"
column summarizing whichever field is set (or "—" for a default/reset row).

## Error handling

- Save: 422 on `InvalidThinkingOverride` (family/field mismatch, or both/neither
  set) — same shape as the existing `InvalidModelName` 422.
- Dispatch-time read failure: unchanged fail-safe — degrades to no override on
  either metadata key.
- No-override default is now always explicit (`thinking_budget=0` or
  `thinking_level="low"`), so tracing is accurate even without an admin override.

## Testing

- Backend: `ThinkingOverride`'s exactly-one-field validator; `validate_extra_config`
  (mismatch rejected both directions, matching pair accepted, `None` accepted as
  no-op); migration idempotency; endpoint tests for save/get/history carrying
  `extra_config`; `resolve_thinking_config`'s three cases (explicit override,
  Gemini-3 default, pre-3 default) and `llm_trace_attributes`' attribute shape —
  both pure, no credentials needed, unlike `build_session` itself.
- Frontend: `isGemini3Model` unit tests; API-layer test extension for the new
  field/parameter; page-level manual check that the control swaps on model-name
  change (no automated page test, matching this codebase's established convention
  of not unit-testing page components).

## Out of scope

- `apps/agent_worker/src/agent_worker/main.py:452-455`'s **separate** Observer
  extraction LLM (`settings.observer_extract_primary_model`, routed through
  `vera_core.llm.ResilientLLM`, out-of-pipeline) *also* hardcodes
  `ThinkingConfig(thinking_budget=0)` unconditionally, and its configured default
  (`"google:gemini-3.5-flash"`) is itself a Gemini-3-family model — meaning this
  exact bug likely already fires there today too. This is a different LLM, a
  different config surface (an env var, not `voice_model_config`), and explicitly
  not part of this admin-override feature — flagged here so it isn't lost, not
  fixed in this change.
- Exposing `thinking_budget`/`thinking_level` for any provider other than Google —
  no other provider exists in the cascade today.
- A DB-level CHECK constraint on `extra_config`'s JSON shape — the single write
  path already validates it.
