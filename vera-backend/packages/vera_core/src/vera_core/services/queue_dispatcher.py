"""Event-driven call queue dispatcher.

Pulls admitted forms from the tenant's queue, checks concurrency limits and
insurance-provider working hours, and initiates calls. Invoked on two events:
(1) a form is enqueued, (2) a call ends and a concurrency slot frees up. Under
browser-callee transport (a test-only gateway flag) no SIP call is placed and the
room simply waits for a browser participant.

A pass runs in two phases (`stage_and_dial` = both). `stage_dispatch` claims the
forms and provisions their INITIATED calls in one transaction; the caller COMMITS;
`place_dials` then dials. Dialing after the commit is load-bearing, not incidental: a
payer IVR answers within a second, and the worker's `call.answered` cannot update a
Call row it can't yet see — dialing inside the staging transaction made that event
lose the race and park for the consumer's 60s XAUTOCLAIM window, stranding the call
at `initiated` in live monitoring.

Mostly PHI-free — it operates on form IDs, statuses, and tenant/provider config. The one
exception is the dispatch `metadata`, which carries `agent_context` (raw patient/provider
identifiers) into LiveKit; never log the `metadata` dict, and never log a traceback from the
dispatch-failure handler — a SQLAlchemy/redis error raised while staging the plan embeds the
statement params. Log the exception type, or for a LiveKit rejection `TelephonyError.diagnostic`
— enough to tell a stale trunk from a downed SIP service without quoting the request back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from opentelemetry import trace
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.audit import AuditRecord
from vera_core.db.rls import tenant_session
from vera_core.forms.call_plan import (
    CallPlan,
    PrefillFuser,
    compile_call_plan,
    focus_call_plan,
)
from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.prompting import PromptDocument
from vera_core.forms.review import REVIEW_CONFIDENCE_FLOOR, focus_paths
from vera_core.integrations.credentials import get_integration_credentials
from vera_core.models import (
    Call,
    CallEvent,
    CallFormSnapshot,
    CallLineage,
    InsuranceProvider,
    PatientForm,
    PromptVersion,
    SchemaVersion,
    Tenant,
)
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.call import TERMINAL_CALL_STATUS_VALUES
from vera_core.models.enums import (
    CallEventType,
    CallMode,
    CallStatus,
    FormStatus,
    VersionStatus,
)
from vera_core.observability.correlation import (
    TRANSPORT_ATTR,
    TRANSPORT_BROWSER,
    TRANSPORT_SIP,
    call_trace_attributes,
    room_name_for_call,
)
from vera_core.schemas import PersonaTweak
from vera_core.services.call_lifecycle import apply_terminal_call_status
from vera_core.services.field_answers import current_values_by_path
from vera_core.services.field_status import load_authoritative_call_ids, load_field_status
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError
from vera_core.services.ivr_selection import (
    add_active_playbook_metadata,
    add_agent_context_metadata,
)
from vera_core.services.model_config import add_llm_model_override_metadata
from vera_core.services.recordings import start_recording_for_call
from vera_core.telephony import OutboundDialError, TelephonyError

if TYPE_CHECKING:
    from uuid import UUID

    from vera_core.audit import AuditSink
    from vera_core.config.kms import KeyManagementService
    from vera_core.plan_store import CallPlanService
    from vera_core.services.recordings import RecordingConfig

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

_EASTERN = ZoneInfo("America/New_York")

# Form statuses that count toward the tenant's max_concurrent_calls ceiling.
# Public: the analytics queue-status endpoint reads this too, to mirror the slot math exactly.
DISPATCH_ACTIVE_FORM_STATUSES = (
    FormStatus.IN_CALL.value,
    FormStatus.AI_PROCESSING.value,
)

_DISPATCH_LOCK_CLASS = 0x76455241  # "vERA" — namespace for dispatch advisory locks

# Per-pass call-plan template memo: schema_version_id -> (per-form fuser over the
# compiled template, prompt_version_id) or None (legacy v1 / compile failure).
type _PlanCache = dict[UUID, tuple[PrefillFuser, UUID | None] | None]


def _now_eastern_time() -> time:
    """Current wall-clock time in US Eastern. Extracted for test patching."""
    return datetime.now(_EASTERN).time()


def is_within_working_hours(provider: InsuranceProvider) -> bool:
    """Return True if *provider* is within its configured working-hours window.

    When either bound is ``None`` the gate is fully open — hours are opt-in.
    """
    if provider.working_hour_start is None or provider.working_hour_end is None:
        return True
    now_time = _now_eastern_time()
    return provider.working_hour_start <= now_time <= provider.working_hour_end


@dataclass(frozen=True)
class StagedCall:
    """One committed INITIATED call awaiting its outbound dial.

    ``browser_callee`` is the transport, carried explicitly rather than inferred from a
    missing ``phone_number``: that column is nullable, and reading a NULL payer number
    as "no dial needed" would report the call as dispatched without ever ringing anyone.
    The tenant's retry knobs are snapshotted here so the dial-failure path needs no
    second tenant read.
    """

    call_id: UUID
    form_id: UUID
    # repr=False: a payer number is PHI-tagged on patient_form, and a frozen dataclass
    # renders every field, so a future `logger.error("…%s", call)` would leak it.
    phone_number: str | None = field(repr=False)
    trunk_id: str | None
    mode: str
    browser_callee: bool
    max_retries: int
    auto_retry_enabled: bool


async def stage_and_dial(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    livekit: Any,
    kms: KeyManagementService | Any,
    *,
    audit: AuditSink | None = None,
    recording: RecordingConfig | None = None,
    dial_pacing_s: float = 1.0,
    plan_service: CallPlanService | None = None,
    retry_floor: int = REVIEW_CONFIDENCE_FLOOR,
) -> int:
    """One full dispatch pass: stage and COMMIT every call, then place the dials.

    See the module docstring for why the phases are separate transactions. Returns the
    number of calls dialed.
    """
    async with tenant_session(sessionmaker, tenant_id) as session:
        staged = await stage_dispatch(
            session,
            tenant_id,
            livekit,
            kms,
            audit=audit,
            plan_service=plan_service,
            retry_floor=retry_floor,
        )
    return await place_dials(
        sessionmaker,
        tenant_id,
        livekit,
        staged,
        audit=audit,
        recording=recording,
        dial_pacing_s=dial_pacing_s,
    )


async def stage_dispatch(
    session: AsyncSession,
    tenant_id: UUID,
    livekit: Any,
    kms: KeyManagementService | Any,
    *,
    audit: AuditSink | None = None,
    plan_service: CallPlanService | None = None,
    retry_floor: int = REVIEW_CONFIDENCE_FLOOR,
) -> list[StagedCall]:
    """Phase one of a dispatch pass: claim queued forms and provision their calls.

    Everything up to (but not including) the outbound dial — slot math, FIFO
    candidate selection, expiry, `form -> IN_CALL`, the INITIATED Call row, the
    staged CallPlan, and the LiveKit room + agent dispatch. The caller commits, then
    hands the returned records to `place_dials`.

    Parameters
    ----------
    session:
        An active ``AsyncSession`` scoped to *tenant_id* (RLS active).
    tenant_id:
        The tenant whose queue to drain.
    livekit:
        A ``LiveKitGateway`` (or duck-typed fake) with ``create_call_room``.
    kms:
        The ``KeyManagementService`` used to open the tenant's sealed outbound
        SIP trunk credential.
    audit:
        Optional ``AuditSink``. When provided, ``QUEUE_EXPIRED`` events are emitted
        for HIPAA evidence (``QUEUE_DISPATCH`` is emitted by ``place_dials``, once
        the call is actually dialed).
    retry_floor:
        Confidence floor for `is_call_confirmed`. Selects the FOCUSED retry ask
        set (`focus_paths`, below) and the field labels embedded in RETRY room
        metadata. Must be the same value the post-call eval uses
        (`settings.post_call_review_floor`) or the two gates measure different
        populations: a field between the two floors triggers a retry that then
        never asks it.
    plan_service:
        Optional ``CallPlanService``. When provided and the form's pinned
        schema is DSL v2, the compiled CallPlan is staged in Redis for the
        worker, the call's ``prompt_version_id`` lineage is stamped, and the
        dispatch metadata carries ``use_call_plan``. Fail-fast: if the plan
        can't be prepared or staged, the form is NOT dispatched (it stays
        IN_QUEUE for a later pass) — the plan-only worker can't serve a
        plan-less call.
    """
    # Serialize dispatch passes per tenant (consumer refill / sweeper / enqueue
    # tasks race otherwise and can over-allocate concurrency slots): the two-int
    # advisory lock is transaction-scoped, so it releases when this phase commits. By
    # then every claimed form is IN_CALL, so a concurrent pass counts it in active_count
    # and still cannot over-allocate while the dials go out.
    # close_call takes row locks without this lock — no ordering inversion, since
    # the advisory lock is always acquired first, at the very start of a pass.
    await session.execute(
        select(func.pg_advisory_xact_lock(_DISPATCH_LOCK_CLASS, func.hashtext(str(tenant_id))))
    )

    # 1. Load tenant config.
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        logger.warning("dispatch: tenant %s not found", tenant_id)
        return []

    # 2. Count active calls (forms currently IN_CALL or AI_PROCESSING).
    active_count: int = (
        await session.execute(
            select(func.count())
            .select_from(PatientForm)
            .where(
                PatientForm.tenant_id == tenant_id,
                PatientForm.status.in_(list(DISPATCH_ACTIVE_FORM_STATUSES)),
            )
        )
    ).scalar_one()

    slots = tenant.max_concurrent_calls - active_count
    if slots <= 0:
        return []

    # 3. Fetch FIFO candidates — FOR UPDATE SKIP LOCKED prevents double-dispatch.
    # The expiry filter is pushed into the DB WHERE clause to use the DB clock,
    # avoiding Python/DB clock skew (HIPAA timestamp-of-record requirement).
    # func.make_interval takes positional args (years, months, weeks, days, hours, ...);
    # SQLAlchemy's func does not forward keyword args, so hours is the 5th positional.
    expiry_interval = func.make_interval(0, 0, 0, 0, tenant.queue_expiry_hours)
    # The working-hours gate is ALSO pushed into the WHERE: filtered after a
    # LIMIT(slots) FIFO fetch, a few stale forms for a closed provider would
    # occupy the whole window and starve every dispatchable form behind them
    # for up to the expiry horizon (head-of-line blocking). Mirrors
    # _resolve_provider + is_within_working_hours: only an ACTIVE provider with
    # both bounds set can be outside its window.
    now_eastern = _now_eastern_time()
    provider_outside_hours = (
        select(InsuranceProvider.id)
        .where(
            InsuranceProvider.name == PatientForm.insurance_provider,
            InsuranceProvider.status == "active",
            InsuranceProvider.working_hour_start.is_not(None),
            InsuranceProvider.working_hour_end.is_not(None),
            or_(
                InsuranceProvider.working_hour_start > now_eastern,
                InsuranceProvider.working_hour_end < now_eastern,
            ),
        )
        .exists()
    )
    candidates = (
        (
            await session.execute(
                select(PatientForm)
                .where(
                    PatientForm.tenant_id == tenant_id,
                    PatientForm.status == FormStatus.IN_QUEUE.value,
                    (PatientForm.enqueued_at.is_(None))
                    | (PatientForm.enqueued_at > func.now() - expiry_interval),
                    ~provider_outside_hours,
                )
                .order_by(PatientForm.enqueued_at.asc())
                .limit(slots)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    # Expired forms (outside the window) are handled in a separate pass so they
    # get the EXPIRED transition and audit without blocking live candidates.
    expired_candidates = (
        (
            await session.execute(
                select(PatientForm)
                .where(
                    PatientForm.tenant_id == tenant_id,
                    PatientForm.status == FormStatus.IN_QUEUE.value,
                    PatientForm.enqueued_at.is_not(None),
                    PatientForm.enqueued_at <= func.now() - expiry_interval,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    sm = FormStateMachine()

    # 4a. Expire stale forms.
    for form in expired_candidates:
        try:
            sm.transition(form, FormStatus.EXPIRED, tenant_max_retries=tenant.max_retries)
            logger.info("dispatch: expired form %s", form.id)
            if audit is not None:
                await audit.emit(
                    AuditRecord(
                        tenant_id=tenant_id,
                        actor_type=ActorType.SYSTEM,
                        actor_label="queue-dispatcher",
                        event_type=AuditEvent.QUEUE_EXPIRED.value,
                        resource_type="patient_form",
                        resource_id=str(form.id),
                    )
                )
        except InvalidTransitionError:
            pass

    staged: list[StagedCall] = []

    # Tenant-level persona overlay — computed once, nested per-call below.
    tweak = (
        PersonaTweak.model_validate(tenant.persona_tweak)
        if tenant.persona_tweak
        else PersonaTweak()
    )
    tweak_fields = tweak.model_dump(exclude_none=True)

    # getattr: livekit is duck-typed, and a gateway without the flag is a SIP gateway.
    browser_callee: bool = getattr(livekit, "browser_callee_transport", False)

    # Resolve the tenant's outbound SIP trunk once for the whole pass — every
    # candidate dials through the same trunk. Skip the lookup entirely when
    # there's nothing to dispatch, or when no SIP call will be placed at all.
    trunk_id: str | None = None
    if candidates and not browser_callee:
        creds = await get_integration_credentials(
            session, kms, integration_type_name="livekit_outbound_trunk_id"
        )
        trunk_id = creds.get("trunk_id") if creds else None
        if not trunk_id:
            # The enqueue gate normally prevents this; config may have changed since.
            logger.warning(
                "dispatch: tenant %s has queued forms but no outbound trunk; leaving queued",
                tenant_id,
            )
            candidates = []

    # Per-pass CallPlan TEMPLATE memo: the template is a pure function of the
    # schema version (+ its published prompt), and one pass typically drains
    # many same-schema forms — compile once per distinct schema version, then
    # fuse each form's intake prefill into its own per-form plan.
    plan_cache: _PlanCache = {}

    # Forms in one pass typically share a schema version — fetch each once.
    schema_versions: dict[UUID, SchemaVersion] = {}

    for form in candidates:
        # 4b. Working-hours re-check at stage time — the SQL gate above used the
        # pass-start clock, and plan compile + room creation can carry the pass past a
        # closing window. Provider reused below for id + playbook.
        provider = await _resolve_provider(session, form)
        if provider is not None and not is_within_working_hours(provider):
            continue

        # Compile (or reuse) the CallPlan OUTSIDE the savepoint below — the
        # compile is CPU-bound and needs two reads, none of which depend on the
        # flushed Call row; keeping it out shortens the row-lock hold.
        staged_plan = (
            await _resolve_call_plan(session, form, plan_cache)
            if plan_service is not None
            else None
        )
        # Fail fast (plan-only worker): a form whose plan can't be prepared can't be
        # served, so mark it CALL_FAILED. An operator manually requeues (CALL_FAILED →
        # IN_QUEUE); enqueued_at is left as-is (inert until then), matching the dial-
        # failure path in place_dials.
        if plan_service is not None and staged_plan is None:
            logger.warning(
                "dispatch: no usable call plan for form %s — marking CALL_FAILED", form.id
            )
            sm.transition(form, FormStatus.CALL_FAILED, tenant_max_retries=tenant.max_retries)
            continue

        # `mode` describes THIS call: "retry" means the question tree was narrowed (set below),
        # never derived from retry_count — a manual requeue resets that budget, so it cannot
        # tell us the call's shape. A budgeted retry that runs FRESH is honestly a full call.
        call_mode = CallMode.FULL
        # Real-call dispatch metadata: the worker must wait for the SIP callee to
        # answer and publish envelope events for live monitoring. IVR navigation is
        # the operator's per-form queue-time choice (voice-lab-style toggle) — when
        # ON, the provider's active playbook (if any) specializes the navigator.
        metadata: dict[str, Any] = {
            "wait_for_speaker": True,
            "publish_events": True,
            "enable_observer": tenant.observer_enabled,
        }
        if form.ivr_navigation_enabled:
            metadata["enable_ivr_navigation"] = True
        if tweak_fields:
            metadata["persona_tweak"] = tweak_fields
        if browser_callee:
            # Tells the worker a browser speaker stands in for an answered SIP callee.
            metadata["browser_callee"] = True

        # Read once, outside the savepoint: the retry-focus gates and the pre-call
        # snapshot below both need the form's current values.
        values = await current_values_by_path(session, form.id)

        # Retry scope: when a CALL captured a reference number, this is a FOCUSED retry — stage a
        # plan narrowed to what no authoritative call has confirmed, so the agent asks ONLY those
        # and never announces a prior call. Otherwise it runs FRESH.
        #
        # Gated on the captured reference number, NOT on `call_mode`: the operator surface passes
        # `manual=True`, which resets `retry_count`, so a form with 152 confirmed answers and a
        # reference on file dispatched as FULL and re-asked everything (spec D4).
        if staged_plan is not None:
            version = schema_versions.get(form.schema_version_id)
            if version is None:
                version = (
                    await session.execute(
                        select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
                    )
                ).scalar_one()
                schema_versions[form.schema_version_id] = version
            # The authoritative-call set IS the scope gate — never the current answer at the
            # reference path. A reviewer editing that field writes `source=human, call_id=None`,
            # which supersedes the call's row, and gating on the current row demoted a fully
            # confirmed form back to a FULL call. A human-typed reference carries no `call_id`,
            # so it still cannot open the focused set on what is really a first call (spec D8).
            # Read off the raw dict: a first call has no reference and must not pay for the
            # document parse or the field-status join to discover that.
            reference_field = version.schema_json.get("rep_call_reference_number_field")
            authoritative = (
                await load_authoritative_call_ids(session, form.id, reference_field=reference_field)
                if reference_field
                else frozenset()
            )
            if authoritative:
                plan, plan_prompt_version_id = staged_plan
                doc = FormSchemaDoc.model_validate(version.schema_json)
                focus = focus_paths(
                    doc,
                    await load_field_status(session, form.id),
                    version.schema_json,
                    floor=retry_floor,
                    values=values,
                    authoritative_calls=authoritative,
                )
                if focus:
                    staged_plan = (
                        focus_call_plan(plan, focus, answers=values),
                        plan_prompt_version_id,
                    )
                    call_mode = CallMode.RETRY

        # 4c. Create the call + room — wrap in try/except so one failure does not
        # roll back successfully-staged calls earlier in the same pass.
        # The plan is staged to Redis (non-transactional) BEFORE create_call_room so
        # it's present when create_call_room dispatches the worker (no read race). If
        # create_call_room then fails, the savepoint rolls the Call row back but the
        # Redis key would leak — `staged_plan_room` lets the except handler clear it.
        staged_plan_room: str | None = None
        try:
            sm.transition(form, FormStatus.IN_CALL, tenant_max_retries=tenant.max_retries)

            # Insert the Call and provision its room inside a savepoint: if
            # create_call_room fails, the savepoint rolls back the flushed Call
            # so a failed dispatch leaves no orphan INITIATED row behind (the
            # form is reverted to IN_QUEUE in the except handler below).
            async with session.begin_nested():
                call = Call(
                    tenant_id=tenant_id,
                    form_id=form.id,
                    current_status=CallStatus.INITIATED.value,
                    mode=call_mode.value,
                    # Whoever enqueued the form owns the call, however late dispatch runs.
                    initiated_by_id=form.enqueued_by_id,
                    insurance_provider_id=provider.id if provider else None,
                    ivr_enabled=form.ivr_navigation_enabled,
                )
                session.add(call)
                await session.flush()
                # before_state must be captured pre-call: answers stream in live during
                # the call, so a closeout-time capture already contains them and the
                # attempt's changed_paths diff would collapse to 0.
                session.add(
                    CallFormSnapshot(
                        tenant_id=tenant_id,
                        call_id=call.id,
                        before_state=values,
                        after_state={},
                    )
                )
                # Any prior call on this form makes this one its retry, whatever the plan's
                # shape — the timeline's "retry of attempt N" must not depend on the label.
                # The retry is SCOPED by the plan itself (a focused retry stages a narrowed
                # plan via focus_call_plan above), never by a prompt overlay — the agent is
                # never told this is a retry, so nothing leaks to the payer rep.
                parent_call_id = (
                    await session.execute(
                        select(Call.id)
                        .where(Call.form_id == form.id, Call.id != call.id)
                        .order_by(Call.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()

                room_name = room_name_for_call(tenant_id, call.id)
                span_attrs: dict[str, Any] = {
                    **call_trace_attributes(room_name, in_call_trace=False),
                    "vera.dispatch.ivr_enabled": bool(metadata.get("enable_ivr_navigation")),
                    TRANSPORT_ATTR: TRANSPORT_BROWSER if browser_callee else TRANSPORT_SIP,
                }
                if staged_plan is not None:
                    span_attrs["vera.dispatch.task_count"] = len(staged_plan[0].tasks)
                # This section builds `metadata` (agent_context, raw PHI) and stages the plan
                # (raw intake values) to Redis — a raised SQLAlchemy/redis error can embed the
                # statement params, so BOTH OTel exception knobs must be off:
                # record_exception=False drops the exception EVENT (message + traceback),
                # set_status_on_exception=False drops the f"{type}: {exc}" status description.
                # create_call_room's own handler re-raises PHI-free for the same reason (below).
                with tracer.start_as_current_span(
                    "vera.dispatch.stage_call",
                    attributes=span_attrs,
                    record_exception=False,
                    set_status_on_exception=False,
                ):
                    if plan_service is not None and staged_plan is not None:
                        plan, plan_prompt_version_id = staged_plan
                        # Fail fast: a staging failure aborts THIS dispatch — the raise
                        # propagates to the except below, which rolls back the Call and
                        # reverts the form to IN_QUEUE. Never place a call whose plan
                        # didn't reach the store (the plan-only worker can't serve it).
                        await plan_service.put(room_name, plan)
                        metadata["use_call_plan"] = True
                        staged_plan_room = room_name  # for orphan cleanup on rollback
                        # Lineage rides the same failure path as the put above: a
                        # staging raise aborts the dispatch, so a call never claims a
                        # prompt version it didn't actually load.
                        call.prompt_version_id = plan_prompt_version_id
                    if form.ivr_navigation_enabled and provider is not None:
                        await add_active_playbook_metadata(session, provider.id, metadata)
                    if form.ivr_navigation_enabled:
                        await add_agent_context_metadata(session, form, metadata)
                    # Unlike the two calls above, this one never raises — a broken config-table
                    # read degrades to the hardcoded default instead of failing the dispatch.
                    await add_llm_model_override_metadata(session, metadata)
                    # This also dispatches the agent, so its _SPEAKER_TIMEOUT_S answer
                    # deadline starts here — not at the dial. A full pass stages every call
                    # before place_dials rings the first, so the last call in a wide pass
                    # spends part of that budget waiting on dial pacing. Moving the room
                    # (and agent) into place_dials is the fix; see the PR discussion.
                    await livekit.create_call_room(room_name, metadata=metadata)
                    session.add(
                        CallEvent(
                            tenant_id=tenant_id,
                            call_id=call.id,
                            event_type=CallEventType.STATUS.value,
                            event_value=CallStatus.INITIATED.value,
                        )
                    )

                    if parent_call_id is not None:
                        session.add(
                            CallLineage(
                                tenant_id=tenant_id,
                                parent_call_id=parent_call_id,
                                retry_call_id=call.id,
                            )
                        )
        except Exception as exc:
            # Never the traceback: a SQLAlchemy/redis error raised while staging the
            # plan embeds the statement params (raw intake values).
            detail = exc.diagnostic if isinstance(exc, TelephonyError) else type(exc).__name__
            # The savepoint rolled back the Call; the staged plan (Redis, non-
            # transactional) did NOT roll back — clear it so a failed dispatch
            # leaves no orphan plan key behind (best-effort; the TTL is the backstop).
            if plan_service is not None and staged_plan_room is not None:
                with contextlib.suppress(Exception):
                    await plan_service.clear(staged_plan_room)
            # Park without spending the retry budget: reverting to IN_QUEUE redialed
            # forever, and charging a clinical retry for an infra blip retires the form.
            sm.transition(form, FormStatus.CALL_FAILED, tenant_max_retries=tenant.max_retries)
            logger.error(
                "dispatch: form %s failed to dispatch (%s) — parked in CALL_FAILED",
                form.id,
                detail,
            )
            continue

        staged.append(
            StagedCall(
                call_id=call.id,
                form_id=form.id,
                phone_number=form.insurance_provider_phone_number,
                trunk_id=trunk_id,
                mode=call_mode.value,
                browser_callee=browser_callee,
                max_retries=tenant.max_retries,
                auto_retry_enabled=tenant.auto_retry_enabled,
            )
        )

    return staged


async def place_dials(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    livekit: Any,
    staged: list[StagedCall],
    *,
    audit: AuditSink | None = None,
    recording: RecordingConfig | None = None,
    dial_pacing_s: float = 1.0,
) -> int:
    """Phase two: dial each staged call — see the module docstring for why it is separate.

    Returns the number of calls dialed; a rejected dial does not count, even though its
    Call row survives as evidence. *dial_pacing_s* separates successive dial STEPS, never
    applied before the first — a failed dial still consumes carrier capacity, and a step
    the row lock skips is rare enough not to be worth distinguishing.
    """
    dispatched = 0
    dialed_once = False
    for call in staged:
        try:
            if not call.browser_callee:
                if dialed_once:
                    await asyncio.sleep(dial_pacing_s)
                dialed_once = True
                if not await _dial(sessionmaker, tenant_id, livekit, call):
                    continue
            dispatched += 1
            await _finish_dispatch(
                sessionmaker, tenant_id, livekit, call, audit=audit, recording=recording
            )
        except Exception as exc:
            # One call's failure must not strand every later staged call undialed — and
            # if its SIP leg is already live, losing the recording or audit row is the
            # lesser harm.
            logger.error(
                "dispatch: call %s did not complete its dial step (%s)",
                call.call_id,
                type(exc).__name__,
            )

    return dispatched


async def _dial(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    livekit: Any,
    call: StagedCall,
) -> bool:
    """Dial one staged call under its row lock, recording a rejection as terminal.

    The lock is what keeps the dial and the row agreeing. Committing the row before the
    dial is the point of the split, but it also makes the call endable in the gap:
    `POST /calls/{id}/end` closes a pre-answer call as CANCELED and deletes its room, and
    an unsynchronized dial would recreate that room and ring the payer into it with no
    agent — a terminal call with a live SIP leg, which no sweeper reconciles. Holding the
    lock across the dial is cheap because `create_sip_participant` is
    `wait_until_answered=False`: it returns once LiveKit accepts the request, long before
    the callee picks up, so no `call.answered` can be waiting behind it. Call then form,
    the same lock order as `close_call`.
    """
    room_name = room_name_for_call(tenant_id, call.call_id)
    dialed = False
    reap_room = False
    async with tenant_session(sessionmaker, tenant_id) as session:
        row = (
            await session.execute(select(Call).where(Call.id == call.call_id).with_for_update())
        ).scalar_one_or_none()
        if row is None or row.current_status in TERMINAL_CALL_STATUS_VALUES:
            logger.info("dispatch: skipping dial for call %s — no longer live", call.call_id)
            return False
        try:
            await livekit.create_sip_participant(room_name, call.phone_number, call.trunk_id)
            dialed = True
        except OutboundDialError as exc:
            # str(exc), not .diagnostic: keeps the detail when there is no code to render.
            logger.warning("dispatch: outbound dial failed for call %s: %s", call.call_id, exc)
            form = (
                await session.execute(
                    select(PatientForm).where(PatientForm.id == call.form_id).with_for_update()
                )
            ).scalar_one()
            if apply_terminal_call_status(
                row,
                form,
                CallStatus.FAILED,
                tenant_max_retries=call.max_retries,
                auto_retry_enabled=call.auto_retry_enabled,
            ):
                form.enqueued_at = func.now()
            row.ended_at = func.now()
            session.add(
                CallEvent(
                    tenant_id=tenant_id,
                    call_id=row.id,
                    event_type=CallEventType.STATUS.value,
                    event_value=CallStatus.FAILED.value,
                )
            )
            reap_room = True
    if reap_room:
        with contextlib.suppress(Exception):  # room teardown is best-effort
            await livekit.delete_room(room_name)
    return dialed


async def _finish_dispatch(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    livekit: Any,
    call: StagedCall,
    *,
    audit: AuditSink | None,
    recording: RecordingConfig | None,
) -> None:
    """Start audio egress and emit the QUEUE_DISPATCH evidence for a dialed call."""
    if recording is not None:
        # Fail-open, after a successful dial: a recording failure must never undo a
        # dispatched call, and a failed dial should not leave an egress recording an
        # empty room.
        async with tenant_session(sessionmaker, tenant_id) as session:
            await start_recording_for_call(
                session,
                livekit,
                config=recording,
                tenant_id=tenant_id,
                call_id=call.call_id,
                audit=audit,
            )
    logger.info(
        "dispatch: initiated call %s for form %s (mode=%s)", call.call_id, call.form_id, call.mode
    )
    if audit is not None:
        await audit.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.SYSTEM,
                actor_label="queue-dispatcher",
                event_type=AuditEvent.QUEUE_DISPATCH.value,
                resource_type="patient_form",
                resource_id=str(call.form_id),
                detail={"call_id": str(call.call_id), "mode": call.mode},
            )
        )


async def _resolve_call_plan(
    session: AsyncSession,
    form: PatientForm,
    cache: _PlanCache,
) -> tuple[CallPlan, UUID | None] | None:
    """The form's fused ``(CallPlan, prompt_version_id)``: the per-schema-version
    TEMPLATE (memoized per pass) + this form's intake-prefilled values hydrated
    in (`PrefillFuser.fuse` — placeholders, the Known-information block, and the
    answers seed for gates/rules).

    ``None`` = no plan: the pinned schema is legacy v1, or a compile/fuse failed.
    The caller fails fast on ``None`` (skips the form, leaving it IN_QUEUE) — the
    plan-only worker can't serve a plan-less call.
    """
    template = await _resolve_plan_template(session, form, cache)
    if template is None:
        return None
    fuser, prompt_version_id = template
    try:
        values = await current_values_by_path(session, form.id)
        # fuser.fuse operates on field_answer values (PHI) and a raised exception can embed
        # them, so BOTH OTel exception knobs must be off: record_exception=False drops the
        # exception EVENT (message + traceback), set_status_on_exception=False drops the
        # f"{type}: {exc}" status description — the same reason the handler below logs a
        # type name only.
        with tracer.start_as_current_span(
            "vera.dispatch.fuse_plan",
            attributes={"vera.dispatch.form_id": str(form.id)},
            record_exception=False,
            set_status_on_exception=False,
        ):
            fused = fuser.fuse(values, current_year=datetime.now(_EASTERN).year)
        return fused, prompt_version_id
    except Exception as exc:
        # field_answer values are PHI — type name only, never the statement/params.
        logger.error(
            "dispatch: prefill fuse failed for form %s (%s) — no plan staged",
            form.id,
            type(exc).__name__,
        )
        return None


async def _resolve_plan_template(
    session: AsyncSession,
    form: PatientForm,
    cache: _PlanCache,
) -> tuple[PrefillFuser, UUID | None] | None:
    """The compiled ``(per-form fuser, prompt_version_id)`` for the form's
    pinned schema version, memoized per dispatch pass (pure per schema version —
    tokens intact until fuse; the fuser precomputes the template-invariant
    lookups once).

    ``None`` = legacy v1 schema or compile failure; the caller fails fast on it
    (the form is not dispatched). No published prompt version is NOT a failure —
    it's a documented fallback: the template compiles with FACTORY_SESSION and
    lineage stays NULL.
    """
    if form.schema_version_id in cache:
        return cache[form.schema_version_id]
    resolved: tuple[PrefillFuser, UUID | None] | None = None
    try:
        schema_version = (
            await session.execute(
                select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
            )
        ).scalar_one_or_none()
        if schema_version is not None and is_v2(schema_version.schema_json):
            doc = FormSchemaDoc.model_validate(schema_version.schema_json)
            prompt_version = (
                await session.execute(
                    select(PromptVersion)
                    .where(
                        PromptVersion.schema_version_id == schema_version.id,
                        PromptVersion.status == VersionStatus.PUBLISHED.value,
                    )
                    # ≤1 published per prompt family (partial unique index); newest
                    # wins if several families target the same schema version.
                    .order_by(PromptVersion.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            prompt_doc = (
                PromptDocument.model_validate(prompt_version.composite_json)
                if prompt_version is not None
                else None
            )
            prompt_version_id = prompt_version.id if prompt_version is not None else None
            # Unlike the fuse_plan / stage_call spans, this one deliberately keeps OTel's
            # exception defaults (record_exception / set_status_on_exception both True):
            # compile_call_plan only touches schema + prompt documents, which are config, not
            # PHI — so a recorded exception is safe and useful here, for the same reason the
            # handler below logs the full traceback.
            with tracer.start_as_current_span(
                "vera.dispatch.compile_plan",
                attributes={"vera.dispatch.schema_version": str(schema_version.id)},
            ):
                plan = compile_call_plan(
                    doc,
                    prompt_doc,
                    schema_version_id=schema_version.id,
                    prompt_version_id=prompt_version_id,
                )
            resolved = (PrefillFuser(doc, plan), prompt_version_id)
    except Exception:
        # Schema/prompt documents are config, not PHI — the traceback is safe.
        logger.exception("dispatch: call plan compile failed for form %s — no plan staged", form.id)
    cache[form.schema_version_id] = resolved
    return resolved


async def _resolve_provider(session: AsyncSession, form: PatientForm) -> InsuranceProvider | None:
    """The form's ACTIVE insurance provider record, or None.

    A missing/unresolved provider is not an error — working-hours enforcement
    and playbook attach are both opt-in and simply skip when it's None.
    """
    if not form.insurance_provider:
        return None
    # Case-insensitive/trimmed match (uses the lower(name) unique index): the send-to-queue
    # picker canonicalizes the string to the exact catalog name, but a form queued without a
    # pick still resolves despite casing/whitespace drift from intake.
    return (
        await session.execute(
            select(InsuranceProvider).where(
                func.lower(InsuranceProvider.name)
                == func.lower(func.trim(form.insurance_provider)),
                InsuranceProvider.status == "active",
            )
        )
    ).scalar_one_or_none()
