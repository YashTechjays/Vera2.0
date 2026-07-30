"""Builders for the LiveKit behavioural evals.

The plan is compiled through the REAL production path — published `schema_version` +
newest published `prompt_version` → `compile_call_plan` → `fuse_prefill` — so the evals
exercise the compiler, prompt resolution and gating, not a hand-written literal. The call
then runs the real entrypoint (`build_agent`) in TEXT mode: IVR navigator → plan tasks →
wrap-up. No room, no SIP, no audio.

Opt-in (`VERA_EVALS_ENABLED=1`, needs Vertex ADC + a seeded local Postgres) and excluded from
`just check` by the `evals` marker, because they call a live LLM. They cannot cover STT
(Deepgram never runs, so a mis-transcription is unreproducible), real DTMF/SIP (`press_keypad`
needs a job context and is mocked), or Observer extraction timing — a live call is still
required before ship.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from typing import Any, NoReturn

import pytest
from _pytest.outcomes import Skipped
from google.genai.types import ThinkingConfig
from judge import CallEvaluator
from livekit.agents import Agent
from livekit.plugins import google
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from agent_worker.agent import build_agent
from agent_worker.observer import ObserverManager, ResilientAnswerExtractor
from agent_worker.plan_runtime import PlanRunController
from vera_core.call_stream import TYPE_TRANSCRIPT, CallStreamEvent
from vera_core.config import EnvSecretProvider
from vera_core.config.settings import get_settings
from vera_core.forms.call_plan import CallPlan, compile_call_plan, focus_call_plan, fuse_prefill
from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.prompting import PromptDocument
from vera_core.llm import FallbackOptions, LLMSpec, ResilientLLM
from vera_core.models.authoring import FormSchema, PromptVersion, SchemaVersion
from vera_core.models.enums import VersionStatus

INSURANCE_TYPE = "infertility_treatment"

# VERA runs the model the voice cascade ships (`cascade.py`) — an eval on a different model
# would not be testing what reps actually get. The simulated rep is scenery, not the subject,
# so it uses the newer tier the Observer already trusts for grounded extraction
# (`settings.observer_extract_primary_model`): it sticks to the fact sheet and invents fewer
# values. Both are paired with thinking_budget=0, as `main.py` does for extraction.
VERA_MODEL = "gemini-3.5-flash"
REP_MODEL = "gemini-3.5-flash"

# One synthetic case, so the IVR menu context and the intake prefill can never contradict
# each other. Nothing here is real patient data and this harness must never be pointed at a
# production database.
CASE = {
    "patient_name": "Test T",
    "patient_dob": "04/12/1991",
    "patient_gender": "Female",
    "spouse_name": "Alex T",
    "spouse_dob": "03/03/1989",
    "member_id": "POL-661522",
    "chart_number": "CH-77",
    "appointment_date": "8/2/2026",
    "appointment_type": "Consultation",
    "doctor_name": "Dr. Jane Smith",
    "doctor_npi": "1982736450",
    "hospital_name": "Demo Health Partners",
    "hospital_address": "1 Demo Way, Springfield",
    "hospital_tax_id": "987654313",
    "hospital_npi": "1122334455",
    "insurance_provider_name": "UnitedHealthcare",
    "insurance_provider_phone_number": "800-555-0100",
    # The compiled intro renders this as the supervising human ("supervised by my human
    # manager, ..."), so it must read as a person, not a system name.
    "verified_by": "Dr. Reyes",
    "callback_number": "800-555-0199",
}

# The `{{token}}` values the IVR navigator reads off to the phone menu.
AGENT_CONTEXT = {
    "patient_name": CASE["patient_name"],
    "patient_dob": CASE["patient_dob"],
    "member_id": CASE["member_id"],
    "doctor_npi": CASE["doctor_npi"],
    "hospital_tax_id": CASE["hospital_tax_id"],
}

DISCLOSURE = "recorded for quality and training purposes"

# The reference call's machine turns, verbatim — UnitedHealthcare's "Avery" virtual assistant.
# Nothing here is a human, so the navigator must NOT hand off during any of it, however
# conversational the menu sounds.
IVR_TURNS = [
    "UnitedHealthcare.",
    (
        "Your call may be monitored or recorded for quality purposes. UnitedHealthcare "
        "provider portal users can now receive real time assistance through our on demand "
        "chat when signing in at u h c provider dot com. Our chat advocates are there to "
        "support with claims, benefits, authorizations, and more. Look for the chat button "
        "and start chatting today. For English, just remain on the line."
    ),
    (
        "Hello. I'm Avery, your virtual assistant at UnitedHealthcare. If you're a member and "
        "need help with your health plan, say I'm a member. Otherwise, in a few words, tell "
        "me what are you calling about."
    ),
    "Are you calling as member or provider? Or press one for member. Press two for provider.",
    "What type of benefits are you calling about?",
    "You can say medical, behavioral health, prescriptions, dentistry.",
    "What is the member ID",
    "I heard POL six six one five two two. Is that correct? Say yes or no.",
    "What is the member's date of birth including the four digit year?",
    "For example, June nineteenth nineteen sixty seven.",
    "What is your NPI",
    "Can you tell me your tax ID number?",
    (
        "United's verification of a member's benefits is not a guarantee of payment, and "
        "United is not entering into a contract for payment of any amount by providing this "
        "information. Payments are only determined when claims are received and processed "
        "through the member's plan. Now what type of benefit are you calling about?"
    ),
    (
        "For example, co pay, coinsurance, therapy benefits and limits, coordination of "
        "benefits, deductible, out of pocket, plan details, or PCP."
    ),
    "Would you like to hear those details again?",
    "If that is all you need, you can hang up now. Otherwise, tell me how I can help.",
    "Are you willing to take a brief survey after this call?",
]

# The one turn that is a live human — a personal name plus an open request for the caller's
# details. This, and only this, may trigger transfer_to_verification.
HUMAN_PICKUP = "Hi, this is Martha in provider services. Who am I speaking with?"


@dataclass(frozen=True)
class Scenario:
    """One call to replay. `facts` is the rep's fact sheet, so a scenario can set up the answers
    a flow rule or contradiction needs; `focus_fields` narrows the compiled plan to just those,
    keeping a rule scenario to a handful of turns instead of a 182-field walk."""

    label: str
    facts: str
    expect_rule: str | None = None
    focus_fields: tuple[str, ...] = ()


class RecordingRunState:
    """Stands in for Redis. Cursor writes are discarded, but extracted answers are kept so a
    test can assert what the Observer actually pulled out of the conversation."""

    def __init__(self) -> None:
        self.recorded: list[tuple[str, Any]] = []

    async def set_active_task(self, room_name: str, task_key: str) -> None:
        return None

    async def record_answer(
        self,
        room_name: str,
        field_path: str,
        *,
        value: Any,
        ts: int,
        confidence: int | None = None,
        evidence_seq: int | None = None,
    ) -> None:
        self.recorded.append((field_path, value))


class NullBus:
    """The Observer emits a CallAnswerRecordedEvent per answer; nothing here consumes them."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class NullTranscript:
    """Required by ObserverManager's constructor but never used: the harness feeds turns
    straight to `ingest()`, so the Redis tail loop (`start()`/`run()`) never runs."""

    def read(
        self, room_name: str, *, first_entry_deadline_s: float | None = None
    ) -> AsyncIterator[tuple[str, CallStreamEvent] | None]:
        async def empty() -> AsyncIterator[tuple[str, CallStreamEvent] | None]:
            return
            yield  # pragma: no cover - makes this an async generator

        return empty()


def judge_strict_enabled() -> bool:
    """By default the evaluator REPORTS and fails nothing: LLM grading flakes, and a flaky gate
    gets ignored, then disabled, then deleted. Set this to make a verified `fail` gate the run."""
    return bool(os.getenv("VERA_EVALS_JUDGE_STRICT"))


def build_evaluator() -> CallEvaluator:
    """The judge goes through vera_core.llm.ResilientLLM — it is an out-of-pipeline LLM call, and
    vera_core/CLAUDE.md mandates that seam for every call outside the live voice cascade."""
    spec = LLMSpec.parse(get_settings().evals_judge_model)
    return CallEvaluator(
        ResilientLLM(
            spec,
            [],  # no fallback tier: the harness must not need an OPENAI_API_KEY
            options=FallbackOptions(attempt_timeout=60.0),
            secrets=EnvSecretProvider(),
        )
    )


def build_observer(controller: PlanRunController, run_state: RecordingRunState) -> ObserverManager:
    """A REAL ObserverManager with the REAL extraction chain, mirroring `main.py`.

    Without it the rule engine can never fire — `RuleEngine.evaluate` has exactly one call site,
    inside the Observer's `_record` — and `gap_fields()` would see every field as unanswered."""
    primary = LLMSpec.parse(get_settings().observer_extract_primary_model)
    if primary.provider == "google":
        primary = replace(primary, extra={"thinking_config": ThinkingConfig(thinking_budget=0)})
    # No fallback tier: the harness must not depend on an OPENAI_API_KEY being present.
    extract_llm = ResilientLLM(
        primary,
        [],
        options=FallbackOptions(
            attempt_timeout=get_settings().observer_extract_attempt_timeout_seconds
        ),
        secrets=EnvSecretProvider(),
    )
    return ObserverManager(
        controller.plan,
        controller=controller,
        run_state=run_state,  # type: ignore[arg-type]
        bus=NullBus(),  # type: ignore[arg-type]
        extractor=ResilientAnswerExtractor(extract_llm),
        transcript=NullTranscript(),  # type: ignore[arg-type]
        room_name="call--eval--1",
    )


def rep_turn(text: str, seq: int) -> CallStreamEvent:
    """The transcript frame the voice pipeline would have published for a rep turn."""
    return CallStreamEvent(
        type=TYPE_TRANSCRIPT, data={"role": "user", "source": "rep", "text": text}, ts=seq
    )


async def settle_observer(manager: ObserverManager) -> None:
    """Await extraction instead of sleeping on it.

    `TaskObserver.feed` schedules a pass as an asyncio task and coalesces mid-flight arrivals
    (`observer.py:_run_passes`), so a completed pass can queue another. Loop until no task is
    pending. A blind sleep would be both slower and racy against a real LLM call."""
    for _ in range(40):
        active = getattr(manager, "_active", None)
        pending = {*getattr(active, "_passes", set()), *getattr(manager, "_closing", set())}
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _skip(reason: str) -> NoReturn:
    """Skip loudly. pytest hides skip reasons unless `-rs` is passed, so an unseeded database
    reads as `10 skipped` with no explanation — a missing fixture that looks like success."""
    print(f"\n===== EVALS SKIPPED: {reason} =====", flush=True)
    pytest.skip(reason)


def full_walk_enabled() -> bool:
    """The full 184-field plan is ~200-300 live LLM calls. Without this, the plan is narrowed
    with `focus_call_plan` — production's own FOCUSED-retry path — for a fast dev loop."""
    return bool(os.getenv("VERA_EVALS_FULL"))


def _intake_values(doc: FormSchemaDoc) -> dict[str, Any]:
    """`{root-anchored path: value}` — the literal stand-in for the dispatcher's
    `current_values_by_path(session, form.id)` field_answer read."""
    system = doc.system_fields or {}
    return {system[key]: value for key, value in CASE.items() if key in system}


async def load_published_plan(
    insurance_type: str = INSURANCE_TYPE, *, focus_fields: Sequence[str] = ()
) -> CallPlan:
    """Compile the plan exactly as dispatch does.

    Mirrors `vera_core/services/queue_dispatcher.py:_resolve_plan_template` step for step —
    published schema version, newest published prompt version (absent is a legal fallback to
    FACTORY_SESSION), `compile_call_plan`, then per-form `fuse_prefill`. Skips rather than
    fails when the local DB has no seeded rows: that is a missing fixture, not a broken agent.
    """
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            schema_version = (
                await conn.execute(
                    select(SchemaVersion)
                    .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
                    .where(
                        FormSchema.insurance_type == insurance_type,
                        SchemaVersion.status == VersionStatus.PUBLISHED.value,
                    )
                    .order_by(SchemaVersion.version.desc())
                    .limit(1)
                )
            ).one_or_none()
            if schema_version is None:
                _skip(
                    f"no published schema_version for {insurance_type} — "
                    "run `just up && just migrate && just seed`"
                )
            prompt_version = (
                await conn.execute(
                    select(PromptVersion)
                    .where(
                        PromptVersion.schema_version_id == schema_version.id,
                        PromptVersion.status == VersionStatus.PUBLISHED.value,
                    )
                    .order_by(PromptVersion.created_at.desc())
                    .limit(1)
                )
            ).one_or_none()
    except Skipped:
        raise  # _skip's own signal — do not relabel it as a connection failure
    except Exception as exc:  # schema/prompt documents are config, not PHI
        _skip(f"cannot reach the local database ({type(exc).__name__}) — run `just up`")
    finally:
        await engine.dispose()

    schema_json = schema_version.schema_json
    if isinstance(schema_json, str):
        schema_json = json.loads(schema_json)
    assert is_v2(schema_json), "eval needs a v2 schema; v1 is not dispatchable"
    doc = FormSchemaDoc.model_validate(schema_json)

    prompt_doc = None
    prompt_version_id = None
    if prompt_version is not None:
        composite = prompt_version.composite_json
        prompt_doc = PromptDocument.model_validate(
            json.loads(composite) if isinstance(composite, str) else composite
        )
        prompt_version_id = prompt_version.id

    plan = compile_call_plan(
        doc,
        prompt_doc,
        schema_version_id=schema_version.id,
        prompt_version_id=prompt_version_id,
    )
    if focus_fields:
        # A rule scenario narrows to exactly the fields its rule reads, plus the closer so the
        # chain can still reach wrap-up.
        paths = [*focus_fields, *(f.path for f in plan.tasks[-1].fields)]
        plan = focus_call_plan(plan, paths)
    elif not full_walk_enabled():
        # Narrow to the opening tasks plus the closer, so the chain still reaches wrap-up.
        keep = [*plan.tasks[:2], plan.tasks[-1]]
        plan = focus_call_plan(plan, [f.path for task in keep for f in task.fields])
    return fuse_prefill(doc, plan, _intake_values(doc), current_year=2026)


def build_llm(model: str = VERA_MODEL) -> google.LLM:
    """The SAME plugin the worker uses (`cascade.py`) — Vertex Gemini, inside the BAA
    boundary. NEVER `livekit.agents.inference.*`: that is LiveKit Cloud, streams off-box,
    and is a bright-line violation (vera-backend/CLAUDE.md)."""
    return google.LLM(
        model=model,
        vertexai=True,
        location="global",
        thinking_config=ThinkingConfig(thinking_budget=0),
    )


async def make_controller(
    scenario: "Scenario | None" = None,
) -> tuple[PlanRunController, RecordingRunState]:
    """Default handoff-context behaviour — the evals cover the call FLOW, not any one carry
    strategy. The gap pass stays on; with a real Observer attached it now sweeps only the fields
    the rep genuinely left unanswered."""
    run_state = RecordingRunState()
    focus = scenario.focus_fields if scenario is not None else ()
    controller = PlanRunController(
        await load_published_plan(focus_fields=focus),
        room_name="call--eval--1",
        run_state=run_state,  # type: ignore[arg-type]
    )
    return controller, run_state


def make_entry_agent(controller: PlanRunController) -> Agent:
    """The call's first agent, chosen the way production chooses it — so the run starts at the
    IVR navigator and reaches the plan only through the real handoff."""
    return build_agent(
        {"enable_ivr_navigation": True, "agent_context": AGENT_CONTEXT}, controller=controller
    )


def carried_text(agent: Agent) -> str:
    """Everything `agent` was handed, as one string: the evals assert that a turn is present,
    not on the shape of the message list."""
    return " ".join(
        content
        for item in agent.chat_ctx.items
        if item.type == "message"
        for content in item.content
        if isinstance(content, str)
    )
