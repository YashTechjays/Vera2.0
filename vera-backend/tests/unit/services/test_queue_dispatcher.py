"""Unit tests for QueueDispatcher.

Uses an in-memory approach: the dispatcher is tested through its public
interface with mock SQLAlchemy session results and a FakeLiveKit, verifying
FIFO ordering, concurrency gating, working-hours checks, and expiry.

The dial-path tests below route `try_dispatch`'s `session.execute()` calls to
canned results through `FakeSession`, which inspects each statement's target
entity (Tenant / the count aggregate / PatientForm-with-LIMIT vs
PatientForm-without / InsuranceProvider) rather than assuming a fixed call
order — this survives try_dispatch's queries being reordered.
"""

import json
import logging
from collections import deque
from datetime import datetime, time
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.config.kms import KeyManagementService
from vera_core.db import uuid7
from vera_core.forms.call_plan import CallPlan, PrefillFuser
from vera_core.forms.call_plan import compile_call_plan as real_compile_call_plan
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.prompting import FACTORY_SESSION, PromptDocument, numbered_questions
from vera_core.forms.review import FieldStatus
from vera_core.models import (
    Call,
    CallEvent,
    CallFormSnapshot,
    FieldAnswer,
    InsuranceProvider,
    PatientForm,
    PromptVersion,
    SchemaVersion,
    Tenant,
    VoiceModelConfig,
)
from vera_core.models.enums import CallStatus, FormStatus, VoiceModelStage
from vera_core.observability.otel_testing import assert_no_phi_values
from vera_core.plan_store import CallPlanService
from vera_core.services import queue_dispatcher
from vera_core.services.queue_dispatcher import is_within_working_hours, try_dispatch
from vera_core.telephony import LiveKitUnavailable, OutboundDialError

IBV_SCHEMA_JSON: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[3] / "data" / "form_schemas" / "ibv_form_standard_v2.json"
    ).read_text(encoding="utf-8")
)

# The compiled IBV plan's whole field set — the FULL plan a fresh (unfocused) dispatch
# stages, and the superset a FOCUSED retry must narrow. Field paths don't depend on the
# prompt document, so the factory-session fallback (prompt_doc=None) is fine here.
_ALL_IBV_PATHS: frozenset[str] = frozenset(
    field.path
    for task in real_compile_call_plan(
        FormSchemaDoc.model_validate(IBV_SCHEMA_JSON),
        None,
        schema_version_id=uuid7(),
        prompt_version_id=None,
    ).tasks
    for field in task.fields
)


class TestIsWithinWorkingHours:
    """Working-hours gate — pure function, no DB."""

    def _provider(
        self,
        start: time | None = None,
        end: time | None = None,
    ) -> MagicMock:
        p = MagicMock()
        p.working_hour_start = start
        p.working_hour_end = end
        return p

    def test_none_hours_means_always_available(self) -> None:
        assert is_within_working_hours(self._provider()) is True

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_within_hours(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(10, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is True

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_outside_hours(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(6, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is False

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_at_boundary_start(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(8, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is True

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_at_boundary_end(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(17, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is True


# ---------------------------------------------------------------------------
# Dial-path fakes: a minimal AsyncSession stand-in + a FakeLiveKit gateway.
# ---------------------------------------------------------------------------


class _Result:
    """Stand-in for a SQLAlchemy `Result` — only the accessors try_dispatch calls."""

    def __init__(self, *, scalar: Any = None, rows: list[Any] | None = None) -> None:
        self._scalar = scalar
        self._rows = rows if rows is not None else []

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalar_one(self) -> Any:
        return self._scalar

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self._rows

    def __iter__(self) -> Any:
        # `load_authoritative_call_ids` iterates `.scalars()` directly (no `.all()`),
        # mirroring real SQLAlchemy's `ScalarResult`.
        return iter(self._rows)


def _bound_value(stmt: Any, column_name: str) -> Any:
    """Pull a bound literal (e.g. `InsuranceProvider.name == "Acme"`) out of a
    statement's WHERE clause by column name — lets the fake resolve which
    provider a `select(InsuranceProvider).where(...)` is asking for."""
    where = stmt.whereclause
    clauses = where.clauses if hasattr(where, "clauses") else [where]
    for clause in clauses:
        if getattr(clause.left, "name", None) == column_name:
            return clause.right.value
    return None


class _NestedTransaction:
    """Stand-in for `session.begin_nested()` — a plain pass-through async
    context manager; the fake session has nothing to roll back."""

    async def __aenter__(self) -> "_NestedTransaction":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class FakeSession:
    """Routes try_dispatch's `execute()` calls to canned results by the
    statement's target entity — Tenant, the count aggregate (no entity),
    PatientForm with a LIMIT (candidates) vs without (expired), and
    InsuranceProvider by name. Order-independent by construction."""

    def __init__(
        self,
        *,
        tenant: Tenant,
        active_count: int = 0,
        candidates: list[PatientForm] | None = None,
        expired: list[PatientForm] | None = None,
        providers: dict[str, InsuranceProvider] | None = None,
        schema_version: SchemaVersion | None = None,
        prompt_version: PromptVersion | None = None,
        field_answers: dict[Any, list[tuple[str, Any]]] | None = None,
        voice_model: VoiceModelConfig | None = None,
    ) -> None:
        self.tenant = tenant
        self.active_count = active_count
        self.candidates = candidates or []
        self.expired = expired or []
        self.providers = providers or {}
        self.schema_version = schema_version
        self.prompt_version = prompt_version
        # form_id -> [(field_path, stored value)] current field_answer rows
        self.field_answers = field_answers or {}
        # add_llm_model_override_metadata's read — None means "no override, use default".
        self.voice_model = voice_model
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        # .get(...): the advisory-lock select (no select_from) omits the "entity"
        # key entirely, unlike select(func.count()).select_from(...) which carries
        # entity=None — both fall through to the same "no mapped entity" branch.
        entity = stmt.column_descriptions[0].get("entity")
        if entity is Tenant:
            return _Result(scalar=self.tenant)
        if entity is InsuranceProvider:
            name = _bound_value(stmt, "name")
            return _Result(scalar=self.providers.get(name))
        if entity is PatientForm:
            # Honor the LIMIT's bound value so slot math (limit(slots)) is
            # actually exercised — presence alone can't catch a wrong slot count.
            if stmt._limit_clause is not None:
                return _Result(rows=self.candidates[: stmt._limit_clause.value])
            return _Result(rows=self.expired)
        # Serves BOTH the plan-staging reads and add_agent_context_metadata's schema
        # load: the defaults (schema_version=None, field_answers={}) mean tests that
        # exercise neither path attach no plan and no context.
        if entity is SchemaVersion:
            # Two query shapes share this entity: the plan-template read selects the
            # full row (name == "SchemaVersion"); the agent-context read selects only
            # the schema_json column (name == "schema_json").
            if stmt.column_descriptions[0].get("name") == "schema_json":
                return _Result(
                    scalar=self.schema_version.schema_json if self.schema_version else None
                )
            return _Result(scalar=self.schema_version)
        if entity is PromptVersion:
            return _Result(scalar=self.prompt_version)
        if entity is FieldAnswer:
            # Three different FieldAnswer queries land here now that the retry-focus
            # block runs on every staged-plan dispatch, not just call_mode==RETRY:
            # current_values_by_path's (field_path, value) pair, load_field_status's
            # 6-column status row, and load_authoritative_call_ids' bare call_id. Only
            # the first is modeled by `field_answers` — a test wanting real status/
            # authoritative-call data monkeypatches those functions directly instead
            # (see test_focused_retry_includes_conditional_fields_when_gate_is_answered).
            # Routed by column NAME, not count (matching the SchemaVersion routing above a
            # few lines up) — a count match on a query that later grows a column would
            # silently misroute it into the wrong shape instead of failing loudly.
            names = [c["name"] for c in stmt.column_descriptions]
            if names == ["field_path", "value"]:
                form_id = _bound_value(stmt, "form_id")
                return _Result(rows=self.field_answers.get(form_id, []))
            return _Result(rows=[])
        if entity is VoiceModelConfig:
            return _Result(scalar=self.voice_model)
        # select(func.count()) / the per-tenant advisory lock — neither has a mapped entity.
        return _Result(scalar=self.active_count)

    def add(self, obj: Any) -> None:
        if hasattr(obj, "id") and obj.id is None:
            obj.id = uuid7()
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()

    def calls_added(self) -> list[Call]:
        return [o for o in self.added if isinstance(o, Call)]

    def call_events_added(self) -> list[CallEvent]:
        return [o for o in self.added if isinstance(o, CallEvent)]


class FakeLiveKit:
    """Minimal LiveKitGateway stand-in — records room creation + SIP dials."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.dispatch_metadata: list[dict[str, object] | None] = []
        self.sip_dials: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []
        self.browser_callee_transport = False
        # When set, the matching method raises it instead of recording the call.
        self.room_error: Exception | None = None
        self.dial_error: Exception | None = None

    async def create_call_room(
        self, room_name: str, metadata: dict[str, object] | None = None
    ) -> None:
        if self.room_error is not None:
            raise self.room_error
        self.created.append(room_name)
        self.dispatch_metadata.append(metadata)

    async def create_sip_participant(
        self, room_name: str, phone_number: str, trunk_id: str
    ) -> None:
        if self.dial_error is not None:
            raise self.dial_error
        self.sip_dials.append((room_name, phone_number, trunk_id))

    async def delete_room(self, room_name: str) -> None:
        self.deleted.append(room_name)


def _tenant(**overrides: Any) -> Tenant:
    defaults: dict[str, Any] = {
        "id": uuid7(),
        "name": "Test Tenant",
        "slug": f"tenant-{uuid7().hex[:8]}",
        "status": "active",
        "max_agents_per_va": 3,
        "max_concurrent_calls": 3,
        "max_retries": 5,
        "queue_expiry_hours": 48,
        "persona_tweak": {},
        "observer_enabled": True,
        "auto_retry_enabled": True,
    }
    defaults.update(overrides)
    return Tenant(**defaults)


def _form(tenant_id: Any, **overrides: Any) -> PatientForm:
    defaults: dict[str, Any] = {
        "id": uuid7(),
        "tenant_id": tenant_id,
        "schema_version_id": uuid7(),
        "status": FormStatus.IN_QUEUE.value,
        "patient_name": "Jane Doe",
        "insurance_provider_phone_number": "+15551234567",
        "insurance_provider": None,
        "retry_count": 0,
        "enqueued_by_id": None,
        "ivr_navigation_enabled": True,
    }
    defaults.update(overrides)
    return PatientForm(**defaults)


@pytest.fixture(autouse=True)
def _stub_credentials(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any] | None]:
    """Default: a trunk is configured. Individual tests override `creds["value"]`."""
    creds: dict[str, dict[str, Any] | None] = {"value": {"trunk_id": "ST_test"}}

    async def _fake_get_integration_credentials(
        session: Any, kms: Any, *, integration_type_name: str
    ) -> dict[str, Any] | None:
        return creds["value"]

    monkeypatch.setattr(
        queue_dispatcher, "get_integration_credentials", _fake_get_integration_credentials
    )
    return creds


async def _dispatch(
    session: FakeSession,
    tenant_id: Any,
    livekit: FakeLiveKit,
    *,
    plan_service: Any = None,
) -> int:
    """try_dispatch(), casting the fakes to their real types — mypy-strict clean,
    mirroring the `cast(AsyncSession, fake)` convention used elsewhere in the
    unit-test suite (e.g. tests/unit/control_plane/test_queueability.py)."""
    return await try_dispatch(
        cast(AsyncSession, session),
        tenant_id,
        livekit,
        cast(KeyManagementService, object()),
        plan_service=cast(CallPlanService | None, plan_service),
    )


async def test_dispatch_dials_the_forms_payer_number(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    tenant = _tenant(persona_tweak={"greeting": "Custom greeting"})
    form = _form(tenant.id, ivr_navigation_enabled=True)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 1
    call = session.calls_added()[0]
    room_name = livekit.created[0]
    assert livekit.sip_dials == [(room_name, "+15551234567", "ST_test")]
    assert call.current_status == CallStatus.INITIATED.value
    assert call.ivr_enabled is True

    metadata = livekit.dispatch_metadata[0]
    assert metadata is not None
    assert metadata["wait_for_speaker"] is True
    assert metadata["publish_events"] is True
    assert metadata["enable_ivr_navigation"] is True
    assert metadata["persona_tweak"] == {"greeting": "Custom greeting"}
    assert metadata["enable_observer"] is True  # tenant default: AI form filling on


async def test_dispatch_snapshots_form_state_before_the_call(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    # before_state must capture the form as it stood BEFORE the call — answers
    # stream in live during the call, so a closeout-time capture already contains
    # them and the attempt's changed_paths diff collapses to 0 (VR2 field-update bug).
    tenant = _tenant()
    form = _form(tenant.id)
    session = FakeSession(
        tenant=tenant,
        candidates=[form],
        field_answers={form.id: [("cov.a", {"value": "x"})]},
    )
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 1
    call = session.calls_added()[0]
    snapshots = [o for o in session.added if isinstance(o, CallFormSnapshot)]
    assert len(snapshots) == 1
    assert snapshots[0].call_id == call.id
    assert snapshots[0].before_state == {"cov.a": "x"}
    assert snapshots[0].after_state == {}


async def test_dispatch_stamps_ivr_disabled_when_form_toggle_off(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    tenant = _tenant()
    form = _form(tenant.id, ivr_navigation_enabled=False)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 1
    call = session.calls_added()[0]
    assert call.ivr_enabled is False
    assert "enable_ivr_navigation" not in (livekit.dispatch_metadata[0] or {})


async def test_create_call_room_failure_scrubs_phi_from_logs(
    caplog: pytest.LogCaptureFixture,
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    # metadata carries agent_context (raw PHI). If create_call_room raises with an error that
    # embeds the request body, the dispatch failure handler must not log it — the raw exception is
    # re-raised PHI-free (chain suppressed), so no PHI reaches the logs.
    tenant = _tenant()
    form = _form(tenant.id, ivr_navigation_enabled=True)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()
    livekit.room_error = RuntimeError("twirp invalid_argument: bad metadata SECRET_PHI_200236789")

    with caplog.at_level(logging.WARNING):
        dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 0  # the dispatch failed
    assert form.status == FormStatus.CALL_FAILED.value  # ...and the form parked, budget intact
    assert "failed to dispatch" in caplog.text  # the failure IS reported...
    assert "SECRET_PHI_200236789" not in caplog.text  # ...without the raw error / request body


async def test_create_call_room_failure_parks_without_spending_a_retry(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    """A dispatch-prep failure parks the form at once and leaves the budget alone.

    Reverting straight to IN_QUEUE let a persistent LiveKit rejection redial the same
    form forever (retry_count stuck at 0). Spending the retry budget instead would
    charge an infrastructure failure to the patient's clinical allowance — three
    transient blips could retire a form that never dialed. max_retries is generous
    here so the budget is demonstrably available and still untouched. Recovery is an
    operator's manual requeue, which resets it (see test_form_state_machine.py).
    """
    tenant = _tenant(max_retries=5)
    form = _form(tenant.id, retry_count=0)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()
    livekit.room_error = RuntimeError("twirp internal: boom")

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 0
    assert form.status == FormStatus.CALL_FAILED.value
    assert form.retry_count == 0


async def test_create_call_room_failure_logs_livekit_error_code(
    caplog: pytest.LogCaptureFixture,
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    """The LiveKit rejection code/status reaches the log so the failure is
    diagnosable, while the error message (which can echo the PHI-bearing request
    body) does not."""
    tenant = _tenant()
    form = _form(tenant.id, ivr_navigation_enabled=True)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()
    livekit.room_error = LiveKitUnavailable(
        "create_call_room failed", code="resource_exhausted", status=429
    )

    with caplog.at_level(logging.WARNING):
        await _dispatch(session, tenant.id, livekit)

    assert "resource_exhausted" in caplog.text
    assert "429" in caplog.text


async def test_dial_failure_logs_livekit_error_code(
    caplog: pytest.LogCaptureFixture,
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    """Same for the dial: a rejected outbound call names the LiveKit code, so a
    stale trunk id (not_found) is distinguishable from a downed SIP service."""
    tenant = _tenant()
    form = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()
    livekit.dial_error = OutboundDialError(
        "create_sip_participant failed", code="not_found", status=404
    )

    with caplog.at_level(logging.WARNING):
        await _dispatch(session, tenant.id, livekit)

    assert "not_found" in caplog.text
    assert "404" in caplog.text


async def test_dial_failure_without_a_livekit_code_still_names_the_failure(
    caplog: pytest.LogCaptureFixture,
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    """A dial that never reached LiveKit carries no code, so `.diagnostic` collapses to
    "unknown" and drops the developer-authored reason. `str(exc)` keeps it — and stays
    PHI-safe, because a TelephonyError's `detail` is developer-authored by contract."""
    tenant = _tenant()
    form = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()
    livekit.dial_error = OutboundDialError("outbound SIP trunk is not configured")

    with caplog.at_level(logging.WARNING):
        await _dispatch(session, tenant.id, livekit)

    assert "outbound SIP trunk is not configured" in caplog.text
    assert "unknown" not in caplog.text


async def test_ivr_navigation_key_absent_when_form_opts_out(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    tenant = _tenant()
    form = _form(tenant.id, ivr_navigation_enabled=False)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 1
    metadata = livekit.dispatch_metadata[0]
    assert metadata is not None
    assert "enable_ivr_navigation" not in metadata
    assert "persona_tweak" not in metadata  # tenant.persona_tweak is empty


async def test_observer_flag_reflects_tenant_setting_when_disabled(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    tenant = _tenant(observer_enabled=False)
    form = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 1
    metadata = livekit.dispatch_metadata[0]
    assert metadata is not None
    # Flag OFF at dispatch is what stops the worker starting the observer.
    assert metadata["enable_observer"] is False


async def test_llm_model_override_carries_into_dispatch_metadata(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    tenant = _tenant()
    form = _form(tenant.id)
    voice_model = VoiceModelConfig(
        stage=VoiceModelStage.LLM, provider="google", model="gemini-3.5-flash"
    )
    session = FakeSession(tenant=tenant, candidates=[form], voice_model=voice_model)
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 1
    metadata = livekit.dispatch_metadata[0]
    assert metadata is not None
    assert metadata["llm_model_override"] == "gemini-3.5-flash"


async def test_llm_model_override_absent_when_never_set(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    tenant = _tenant()
    form = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form])  # voice_model defaults to None
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 1
    metadata = livekit.dispatch_metadata[0]
    assert metadata is not None
    assert "llm_model_override" not in metadata


async def test_dispatch_without_trunk_leaves_forms_queued(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    _stub_credentials["value"] = None
    tenant = _tenant()
    form = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 0
    assert form.status == FormStatus.IN_QUEUE.value
    assert livekit.created == []
    assert livekit.sip_dials == []


async def test_browser_callee_dispatches_without_a_trunk_and_never_dials(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    _stub_credentials["value"] = None  # no trunk: the SIP path would leave the form queued
    tenant = _tenant()
    form = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()
    livekit.browser_callee_transport = True

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 1
    assert len(livekit.created) == 1
    assert livekit.sip_dials == []
    metadata = livekit.dispatch_metadata[0]
    assert metadata is not None
    assert metadata["browser_callee"] is True


async def test_sip_transport_still_needs_a_trunk(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    """Regression guard: the default SIP path is unaffected by the browser-callee branch."""
    _stub_credentials["value"] = None
    tenant = _tenant()
    form = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 0
    assert livekit.created == []


async def test_dial_failure_marks_call_failed_and_requeues_form(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    tenant = _tenant()
    form = _form(tenant.id, retry_count=0)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()
    livekit.dial_error = OutboundDialError("fake provider rejected the call")

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 0
    call = session.calls_added()[0]
    assert call.current_status == CallStatus.FAILED.value
    events = session.call_events_added()
    assert any(e.event_value == CallStatus.FAILED.value for e in events)
    assert livekit.deleted == livekit.created  # the room was torn down
    assert form.status == FormStatus.IN_QUEUE.value
    assert form.retry_count == 1


async def test_dial_failure_does_not_requeue_when_tenant_auto_retry_disabled(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    """Tenant auto-retry OFF suppresses the failed-dial redial: the call is
    recorded FAILED and the form parks in CALL_FAILED with its budget intact."""
    tenant = _tenant(auto_retry_enabled=False)
    form = _form(tenant.id, retry_count=0)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()
    livekit.dial_error = OutboundDialError("fake provider rejected the call")

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 0
    assert session.calls_added()[0].current_status == CallStatus.FAILED.value
    assert form.status == FormStatus.CALL_FAILED.value
    assert form.retry_count == 0


async def test_dials_are_paced_one_second_apart(
    _stub_credentials: dict[str, dict[str, Any] | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = _tenant(max_concurrent_calls=5)
    form_a = _form(tenant.id)
    form_b = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form_a, form_b])
    livekit = FakeLiveKit()

    sleeps: deque[float] = deque()

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(queue_dispatcher.asyncio, "sleep", _fake_sleep)  # type: ignore[attr-defined]

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 2
    assert list(sleeps) == [1.0]


async def test_slots_come_from_tenant_ceiling_not_per_va_knob(
    _stub_credentials: dict[str, dict[str, Any] | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatcher's tenant-wide slot math reads max_concurrent_calls; the
    per-VA knob (max_agents_per_va) is enforced at enqueue time, not here."""
    tenant = _tenant(max_agents_per_va=1, max_concurrent_calls=2)
    form_a = _form(tenant.id)
    form_b = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form_a, form_b])
    livekit = FakeLiveKit()

    async def _fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(queue_dispatcher.asyncio, "sleep", _fake_sleep)  # type: ignore[attr-defined]

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 2  # per-VA knob of 1 must NOT cap the tenant-wide pass


async def test_pacing_applies_to_failed_dial_attempts(
    _stub_credentials: dict[str, dict[str, Any] | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = _tenant(max_concurrent_calls=5)
    form_a = _form(tenant.id)  # This one will fail to dial
    form_b = _form(tenant.id)  # This one will succeed
    session = FakeSession(tenant=tenant, candidates=[form_a, form_b])
    livekit = FakeLiveKit()

    sleeps: deque[float] = deque()

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(queue_dispatcher.asyncio, "sleep", _fake_sleep)  # type: ignore[attr-defined]

    # First dial attempt fails, second succeeds.
    call_count = {"count": 0}

    async def _create_sip_with_error_toggle(
        room_name: str, phone_number: str, trunk_id: str
    ) -> None:
        call_count["count"] += 1
        if call_count["count"] == 1:
            raise OutboundDialError("fake provider rejected the call")
        livekit.sip_dials.append((room_name, phone_number, trunk_id))

    livekit.create_sip_participant = _create_sip_with_error_toggle  # type: ignore[method-assign]

    dispatched = await _dispatch(session, tenant.id, livekit)

    # Only the second dial succeeds, so dispatched == 1.
    # But pacing should still have occurred between attempts, so sleeps == [1.0].
    assert dispatched == 1
    assert list(sleeps) == [1.0]


# ---------------------------------------------------------------------------
# Call-plan staging: compile + store the CallPlan at dispatch, stamp lineage.
# ---------------------------------------------------------------------------


class FakeCallPlanService:
    """Records plan puts; `fail=True` simulates a Redis outage."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, CallPlan]] = []
        self.fail = False

    async def put(self, room_name: str, plan: CallPlan) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.puts.append((room_name, plan))


def _schema_version(schema_json: dict[str, Any]) -> SchemaVersion:
    return SchemaVersion(
        id=uuid7(),
        schema_id=uuid7(),
        version=1,
        schema_json=schema_json,
        status="published",
    )


def _prompt_version(sv: SchemaVersion) -> PromptVersion:
    return PromptVersion(
        id=uuid7(),
        prompt_id=uuid7(),
        schema_version_id=sv.id,
        composite_json={
            "kind": "prompt_document",
            "session": {"persona": "P.", "goal": "G.", "base_instructions": "B."},
        },
        status="published",
    )


class TestCallPlanStaging:
    async def test_published_prompt_version_stamps_lineage_and_stages_plan(
        self, _stub_credentials: dict[str, dict[str, Any] | None]
    ) -> None:
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        pv = _prompt_version(sv)
        form = _form(tenant.id, schema_version_id=sv.id)
        session = FakeSession(
            tenant=tenant, candidates=[form], schema_version=sv, prompt_version=pv
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        dispatched = await _dispatch(session, tenant.id, livekit, plan_service=plans)

        assert dispatched == 1
        metadata = livekit.dispatch_metadata[0]
        assert metadata is not None and metadata["use_call_plan"] is True
        room_name, plan = plans.puts[0]
        assert room_name == livekit.created[0]
        assert plan.schema_version_id == sv.id
        assert plan.prompt_version_id == pv.id
        assert plan.session.persona == "P."  # operator document, not factory
        assert session.calls_added()[0].prompt_version_id == pv.id

    async def test_focused_retry_includes_conditional_fields_when_gate_is_answered(
        self,
        _stub_credentials: dict[str, dict[str, Any] | None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A focused retry must still ask conditional fields whose gate parent is
        answered: the dispatcher passes real values so eq-gates evaluate, instead of a
        sentinel that reads every value-gate as unmatched and drops its dependents
        (issue 6)."""
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        pv = _prompt_version(sv)
        form = _form(tenant.id, schema_version_id=sv.id, retry_count=1)
        session = FakeSession(
            tenant=tenant, candidates=[form], schema_version=sv, prompt_version=pv
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        ref = "sections.insurance_representative.call_reference_number"
        gate = "sections.pharmacy_benefit_manager.pbm_exists"
        call_id = uuid7()

        async def _status(_session: Any, _form_id: Any) -> dict[str, FieldStatus]:
            # Reference captured BY A CALL → the retry is FOCUSED; the PBM gate is answered.
            return {
                ref: FieldStatus(
                    source="ai_call", ai_supported=True, ai_confidence=96, call_id=call_id
                ),
                gate: FieldStatus(source="ai_call", ai_supported=None, ai_confidence=85),
            }

        async def _values(_session: Any, _form_id: Any) -> dict[str, Any]:
            return {ref: "ABC-123", gate: "Yes"}

        async def _authoritative(
            _session: Any, _form_id: Any, *, reference_field: str
        ) -> frozenset[Any]:
            return frozenset({call_id})

        monkeypatch.setattr(queue_dispatcher, "load_field_status", _status)
        monkeypatch.setattr(queue_dispatcher, "current_values_by_path", _values)
        monkeypatch.setattr(queue_dispatcher, "load_authoritative_call_ids", _authoritative)

        dispatched = await _dispatch(session, tenant.id, livekit, plan_service=plans)

        assert dispatched == 1
        _room, plan = plans.puts[0]
        staged = {f.path for t in plan.tasks for f in t.fields}
        # Guard against silent vacuity (round-2 finding): these two assertions alone pass
        # against the FULL plan too, so pin that focusing actually narrowed the set.
        assert staged < _ALL_IBV_PATHS
        assert "sections.pharmacy_benefit_manager.pbm_name" in staged
        assert "sections.pharmacy_benefit_manager.pbm_phone" in staged

    async def test_no_published_prompt_version_falls_back_to_factory_session(
        self, _stub_credentials: dict[str, dict[str, Any] | None]
    ) -> None:
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        form = _form(tenant.id, schema_version_id=sv.id)
        session = FakeSession(tenant=tenant, candidates=[form], schema_version=sv)
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        dispatched = await _dispatch(session, tenant.id, livekit, plan_service=plans)

        assert dispatched == 1
        metadata = livekit.dispatch_metadata[0]
        assert metadata is not None and metadata["use_call_plan"] is True
        _room, plan = plans.puts[0]
        assert plan.prompt_version_id is None
        assert plan.session.persona == FACTORY_SESSION.persona
        assert session.calls_added()[0].prompt_version_id is None

    async def test_v1_schema_is_skipped_and_marked_call_failed(
        self, _stub_credentials: dict[str, dict[str, Any] | None]
    ) -> None:
        # Plan-only fail-fast: a v1 schema compiles no plan, so the worker can't
        # serve it — the form is NOT dispatched. It is marked CALL_FAILED (failed
        # worklist) instead of looping IN_QUEUE; no call placed, no retry spent.
        tenant = _tenant()
        sv = _schema_version({"patient_information": {"required": []}})  # legacy v1
        form = _form(tenant.id, schema_version_id=sv.id)
        session = FakeSession(tenant=tenant, candidates=[form], schema_version=sv)
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        dispatched = await _dispatch(session, tenant.id, livekit, plan_service=plans)

        assert dispatched == 0  # no plan-less call placed
        assert livekit.dispatch_metadata == []
        assert plans.puts == []
        assert form.status == FormStatus.CALL_FAILED.value
        assert form.retry_count == 0  # not a retry — no budget spent

    async def test_plan_store_failure_aborts_dispatch(
        self, _stub_credentials: dict[str, dict[str, Any] | None]
    ) -> None:
        # Fail-fast: a staging (Redis) failure aborts the dispatch — the Call is
        # rolled back and the form reverts to IN_QUEUE; no call is placed.
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        pv = _prompt_version(sv)
        form = _form(tenant.id, schema_version_id=sv.id)
        session = FakeSession(
            tenant=tenant, candidates=[form], schema_version=sv, prompt_version=pv
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()
        plans.fail = True

        dispatched = await _dispatch(session, tenant.id, livekit, plan_service=plans)

        assert dispatched == 0  # the call does NOT go out
        assert livekit.dispatch_metadata == []  # staging fails before create_call_room
        assert form.status == FormStatus.CALL_FAILED.value  # parked, not reverted for retry

    async def test_no_plan_service_keeps_legacy_metadata(
        self, _stub_credentials: dict[str, dict[str, Any] | None]
    ) -> None:
        tenant = _tenant()
        form = _form(tenant.id)
        session = FakeSession(tenant=tenant, candidates=[form])
        livekit = FakeLiveKit()

        dispatched = await _dispatch(session, tenant.id, livekit)

        assert dispatched == 1
        metadata = livekit.dispatch_metadata[0]
        assert metadata is not None and "use_call_plan" not in metadata

    async def test_prefill_values_are_fused_into_the_staged_plan(
        self, _stub_credentials: dict[str, dict[str, Any] | None]
    ) -> None:
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        form = _form(tenant.id, schema_version_id=sv.id)
        patient_name_path = IBV_SCHEMA_JSON["system_fields"]["patient_name"]
        session = FakeSession(
            tenant=tenant,
            candidates=[form],
            schema_version=sv,
            field_answers={form.id: [(patient_name_path, {"value": "Jane Doe"})]},
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        dispatched = await _dispatch(session, tenant.id, livekit, plan_service=plans)

        assert dispatched == 1
        _room, plan = plans.puts[0]
        assert plan.prefilled == {patient_name_path: "Jane Doe"}
        intro = next(t for t in plan.tasks if t.task_key == "introduction").intro
        assert intro is not None and "Jane Doe" in intro
        assert "{{patient_name}}" not in intro
        assert plan.known_information is not None
        assert "Patient Name: Jane Doe" in plan.known_information

    async def test_template_compiles_once_per_schema_but_fuses_per_form(
        self, _stub_credentials: dict[str, dict[str, Any] | None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = _tenant(max_concurrent_calls=5)
        sv = _schema_version(IBV_SCHEMA_JSON)
        form_a = _form(tenant.id, schema_version_id=sv.id)
        form_b = _form(tenant.id, schema_version_id=sv.id)
        patient_name_path = IBV_SCHEMA_JSON["system_fields"]["patient_name"]
        session = FakeSession(
            tenant=tenant,
            candidates=[form_a, form_b],
            schema_version=sv,
            field_answers={
                form_a.id: [(patient_name_path, {"value": "Jane Doe"})],
                form_b.id: [(patient_name_path, {"value": "John Roe"})],
            },
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        compile_calls = {"count": 0}

        def counting_compile(*args: Any, **kwargs: Any) -> Any:
            compile_calls["count"] += 1
            return real_compile_call_plan(*args, **kwargs)

        monkeypatch.setattr(queue_dispatcher, "compile_call_plan", counting_compile)
        monkeypatch.setattr(queue_dispatcher.asyncio, "sleep", _noop_sleep)  # type: ignore[attr-defined]

        dispatched = await _dispatch(session, tenant.id, livekit, plan_service=plans)

        assert dispatched == 2
        assert compile_calls["count"] == 1  # template memoized per schema version
        fused_names = {
            next(t for t in plan.tasks if t.task_key == "introduction").intro or ""
            for _room, plan in plans.puts
        }
        assert any("Jane Doe" in intro for intro in fused_names)
        assert any("John Roe" in intro for intro in fused_names)

    # Both boolean branches are exercised: `vera.dispatch.ivr_enabled` mirrors the form's
    # opt-in, and _form()'s default is True — so the False case must override it explicitly.
    @pytest.mark.parametrize("ivr_enabled", [False, True])
    async def test_stage_call_span_carries_correlation_and_counts(
        self,
        _stub_credentials: dict[str, dict[str, Any] | None],
        otel_spans: Any,
        ivr_enabled: bool,
    ) -> None:
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        pv = _prompt_version(sv)
        form = _form(tenant.id, schema_version_id=sv.id, ivr_navigation_enabled=ivr_enabled)
        session = FakeSession(
            tenant=tenant, candidates=[form], schema_version=sv, prompt_version=pv
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        await _dispatch(session, tenant.id, livekit, plan_service=plans)

        room_name = livekit.created[0]
        span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.dispatch.stage_call"
        )
        assert span.attributes["vera.room"] == room_name
        assert span.attributes["vera.tenant_id"] == str(tenant.id)
        assert "vera.dispatch.task_count" in span.attributes
        assert span.attributes["vera.dispatch.ivr_enabled"] is ivr_enabled
        # PHI guardrail (design §8): this span wraps the plan staging and the metadata build
        # (agent_context), both of which handle the patient's name — none of it may ride along.
        assert_no_phi_values(span, "Jane Doe")  # _form()'s patient_name

    async def test_compile_and_fuse_spans_are_schema_and_form_scoped(
        self, _stub_credentials: dict[str, dict[str, Any] | None], otel_spans: Any
    ) -> None:
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        form = _form(tenant.id, schema_version_id=sv.id)
        patient_name_path = IBV_SCHEMA_JSON["system_fields"]["patient_name"]
        session = FakeSession(
            tenant=tenant,
            candidates=[form],
            schema_version=sv,
            # A real prefill value so the fuse span is asserted against PHI actually in flight.
            field_answers={form.id: [(patient_name_path, {"value": "Jane Doe"})]},
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        await _dispatch(session, tenant.id, livekit, plan_service=plans)

        # next() raises StopIteration if the span was never emitted — presence is asserted
        # by these lookups, so no separate "span name exists" assertion is needed.
        spans = otel_spans.get_finished_spans()
        compile_span = next(s for s in spans if s.name == "vera.dispatch.compile_plan")
        fuse_span = next(s for s in spans if s.name == "vera.dispatch.fuse_plan")
        assert compile_span.attributes["vera.dispatch.schema_version"] == str(sv.id)
        assert fuse_span.attributes["vera.dispatch.form_id"] == str(form.id)
        assert "vera.room" not in compile_span.attributes  # room_name doesn't exist yet here
        assert "vera.room" not in fuse_span.attributes
        # PHI guardrail (design §8). compile_plan only ever sees schema/prompt config, so it
        # keeps OTel's exception defaults — but the denylist assertion still applies to it.
        assert_no_phi_values(compile_span, "Jane Doe")
        assert_no_phi_values(fuse_span, "Jane Doe")

    async def test_a_form_with_a_captured_reference_dispatches_focused(
        self,
        _stub_credentials: dict[str, dict[str, Any] | None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The defect this fixes: the operator surface (`PUT /patient-forms/{id}/status`)
        resets `retry_count` to 0, so gating the focus decision on `call_mode` left a form
        with a captured reference number — and everything else already confirmed — dispatch
        as a FULL call that re-asked everything. The gate is the reference number alone."""
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        pv = _prompt_version(sv)
        form = _form(tenant.id, schema_version_id=sv.id)  # retry_count deliberately 0
        session = FakeSession(
            tenant=tenant, candidates=[form], schema_version=sv, prompt_version=pv
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        ref = "sections.insurance_representative.call_reference_number"
        call_id = uuid7()

        async def _status(_session: Any, _form_id: Any) -> dict[str, FieldStatus]:
            return {
                ref: FieldStatus(
                    source="ai_call", ai_supported=True, ai_confidence=96, call_id=call_id
                )
            }

        async def _values(_session: Any, _form_id: Any) -> dict[str, Any]:
            return {ref: "ABC-123"}

        async def _authoritative(
            _session: Any, _form_id: Any, *, reference_field: str
        ) -> frozenset[Any]:
            return frozenset({call_id})

        monkeypatch.setattr(queue_dispatcher, "load_field_status", _status)
        monkeypatch.setattr(queue_dispatcher, "current_values_by_path", _values)
        monkeypatch.setattr(queue_dispatcher, "load_authoritative_call_ids", _authoritative)

        dispatched = await _dispatch(session, tenant.id, livekit, plan_service=plans)

        assert dispatched == 1
        _room, plan = plans.puts[0]
        staged = {f.path for t in plan.tasks for f in t.fields}
        assert staged < _ALL_IBV_PATHS  # narrowed
        assert form.retry_count == 0  # the gate is the reference number, not the counter

    async def test_a_form_with_no_captured_reference_dispatches_the_full_plan(
        self, _stub_credentials: dict[str, dict[str, Any] | None]
    ) -> None:
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        pv = _prompt_version(sv)
        form = _form(tenant.id, schema_version_id=sv.id)
        session = FakeSession(
            tenant=tenant, candidates=[form], schema_version=sv, prompt_version=pv
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        dispatched = await _dispatch(session, tenant.id, livekit, plan_service=plans)

        assert dispatched == 1
        _room, plan = plans.puts[0]
        staged = {f.path for t in plan.tasks for f in t.fields}
        assert staged == _ALL_IBV_PATHS

    async def test_the_introduction_and_wrap_up_tasks_survive_the_narrowing(
        self,
        _stub_credentials: dict[str, dict[str, Any] | None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every IBV `collected_per="call"` leaf — not just the reference number — is
        authoritatively confirmed here: `is_insurance_active` (introduction) and `rep_name` +
        `call_reference_number` (wrap_up) all drop out of the required-and-unconfirmed set, so
        the only thing keeping either task alive is `focus_paths` unioning in
        `doc.collected_per_call_paths()`. Confirming only the reference number (as an earlier
        version of this test did) leaves the other two leaves required-and-unconfirmed, which
        pulls them into the focus set through the ordinary required-fields path regardless of
        the union — making the assertion pass even with the union deleted. That was verified by
        mutation (see task-5-report.md) and is why every per-call leaf is confirmed below."""
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        pv = _prompt_version(sv)
        form = _form(tenant.id, schema_version_id=sv.id)
        session = FakeSession(
            tenant=tenant, candidates=[form], schema_version=sv, prompt_version=pv
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        is_insurance_active = "sections.patient_verification.is_insurance_active"
        rep_name = "sections.insurance_representative.rep_name"
        ref = "sections.insurance_representative.call_reference_number"
        call_id = uuid7()

        def _confirmed() -> FieldStatus:
            return FieldStatus(
                source="ai_call", ai_supported=True, ai_confidence=96, call_id=call_id
            )

        async def _status(_session: Any, _form_id: Any) -> dict[str, FieldStatus]:
            return {
                is_insurance_active: _confirmed(),
                rep_name: _confirmed(),
                ref: _confirmed(),
            }

        async def _values(_session: Any, _form_id: Any) -> dict[str, Any]:
            return {is_insurance_active: "Yes", rep_name: "Jane Rep", ref: "ABC-123"}

        async def _authoritative(
            _session: Any, _form_id: Any, *, reference_field: str
        ) -> frozenset[Any]:
            return frozenset({call_id})

        monkeypatch.setattr(queue_dispatcher, "load_field_status", _status)
        monkeypatch.setattr(queue_dispatcher, "current_values_by_path", _values)
        monkeypatch.setattr(queue_dispatcher, "load_authoritative_call_ids", _authoritative)

        dispatched = await _dispatch(session, tenant.id, livekit, plan_service=plans)

        assert dispatched == 1
        _room, plan = plans.puts[0]
        keys = {t.task_key for t in plan.tasks}
        assert {"introduction", "wrap_up"} <= keys

    async def test_the_staged_retry_prompt_is_narrowed_not_just_its_fields(
        self,
        _stub_credentials: dict[str, dict[str, Any] | None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A defect this branch already shipped once: narrowing the tracked FIELD set left the
        SPOKEN question tree — what the rep actually hears — byte-identical to an unfocused call.
        This proves the plan the dispatcher actually stages is narrower where `focus_paths` says
        it should be, on two tasks chosen because this scenario is built to narrow them:
        `diagnostic_coverage`'s top coverage gate stays open while every per-code follow-up
        already has a value on file (so `focus_call_plan`'s gate-closure explosion has nothing
        left to pull back in), and `financial`'s deductible/out-of-pocket groups are FULLY
        confirmed (so group-expansion can't re-admit them) while its lifetime-maximum leaves are
        only PARTLY confirmed. The comparison plan is an independent compile+fuse of the same
        schema/prompt/values — never the staged plan itself — so `staged is full` can never hold
        and the assertions can't go vacuous by construction."""
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        pv = _prompt_version(sv)
        form = _form(tenant.id, schema_version_id=sv.id)
        session = FakeSession(
            tenant=tenant, candidates=[form], schema_version=sv, prompt_version=pv
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        doc = FormSchemaDoc.model_validate(IBV_SCHEMA_JSON)
        ref = doc.rep_call_reference_number_field
        codes = ["58340", "82670", "83001", "83002", "84146", "84443", "84144", "76830"]

        values: dict[str, Any] = {ref: "ABC-123", "sections.lifetime_maximum.total": "$50,000"}
        for code in codes:
            base = f"sections.diagnostic_testing.labs_xray_ultrasound.cpt_{code}"
            values[f"{base}.covered"] = "No"
            values[f"{base}.copay"] = "$0"
            values[f"{base}.coinsurance"] = "0%"
            values[f"{base}.prior_auth"] = "N/A"

        call_id = uuid7()

        def confirmed() -> FieldStatus:
            return FieldStatus(
                source="ai_call", ai_supported=True, ai_confidence=96, call_id=call_id
            )

        status: dict[str, FieldStatus] = {ref: confirmed()}
        for path in (
            "sections.deductibles.individual.total",
            "sections.deductibles.individual.met_amount",
            "sections.deductibles.individual.remaining",
            "sections.out_of_pocket.individual.total",
            "sections.out_of_pocket.individual.met_amount",
            "sections.out_of_pocket.individual.remaining",
            "sections.lifetime_maximum.total",
            "sections.lifetime_maximum.met_amount",
        ):
            status[path] = confirmed()
        for code in codes:
            status[f"sections.diagnostic_testing.labs_xray_ultrasound.cpt_{code}.covered"] = (
                confirmed()
            )

        async def _status(_session: Any, _form_id: Any) -> dict[str, FieldStatus]:
            return status

        async def _values(_session: Any, _form_id: Any) -> dict[str, Any]:
            return values

        async def _authoritative(
            _session: Any, _form_id: Any, *, reference_field: str
        ) -> frozenset[Any]:
            return frozenset({call_id})

        monkeypatch.setattr(queue_dispatcher, "load_field_status", _status)
        monkeypatch.setattr(queue_dispatcher, "current_values_by_path", _values)
        monkeypatch.setattr(queue_dispatcher, "load_authoritative_call_ids", _authoritative)

        dispatched = await _dispatch(session, tenant.id, livekit, plan_service=plans)

        assert dispatched == 1
        _room, staged = plans.puts[0]

        prompt_doc = PromptDocument.model_validate(pv.composite_json)
        full_template = real_compile_call_plan(
            doc, prompt_doc, schema_version_id=sv.id, prompt_version_id=pv.id
        )
        full = PrefillFuser(doc, full_template).fuse(
            values, current_year=datetime.now(ZoneInfo("America/New_York")).year
        )
        assert staged is not full
        full_by_key = {t.task_key: t for t in full.tasks}

        # Strict `<`, never `<=`: a question-count or byte-identical row on either of these two
        # tasks IS the regression this test exists to catch, not something to relax the check for.
        for task_key in ("financial", "diagnostic_coverage"):
            staged_task = next(t for t in staged.tasks if t.task_key == task_key)
            full_task = full_by_key[task_key]
            assert numbered_questions(staged_task.panels) < numbered_questions(full_task.panels), (
                task_key
            )
            assert len(staged_task.prompt) < len(full_task.prompt), task_key


async def _noop_sleep(seconds: float) -> None:
    return None
