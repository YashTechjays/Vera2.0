"""Integration tests for /api/v1/analytics — counts only, real RLS + RBAC."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.control_plane.conftest import RBACWorld, seed_call
from vera_core.db import uuid7
from vera_core.models import Call, InterventionEvent, PatientForm, Tenant
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import InsuranceType

QUEUE_STATUS_PATH = "/api/v1/analytics/queue-status"
LIVE_PATH = "/api/v1/analytics/live"
REPORT_PATH = "/api/v1/analytics/report"
FILTERS_PATH = "/api/v1/analytics/filters"

_FROM = datetime(2026, 1, 8, tzinfo=UTC)
_TO = datetime(2026, 1, 15, tzinfo=UTC)
_PREV = datetime(2026, 1, 3, tzinfo=UTC)  # inside the previous 7-day window


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _delete_forms(sessionmaker: async_sessionmaker[AsyncSession], *form_ids: UUID) -> None:
    async with sessionmaker() as session, session.begin():
        await session.execute(delete(PatientForm).where(PatientForm.id.in_(form_ids)))


async def _delete_calls_and_forms(
    sessionmaker: async_sessionmaker[AsyncSession], *form_ids: UUID
) -> None:
    async with sessionmaker() as session, session.begin():
        await session.execute(delete(Call).where(Call.form_id.in_(form_ids)))
    await _delete_forms(sessionmaker, *form_ids)


async def _seed_form(session: AsyncSession, tenant_id: UUID) -> UUID:
    """A minimal PatientForm satisfying `schema_version_id` NOT NULL, for queue-status
    counts. Mirrors `test_call_queue._seed_ready_form`'s schema find-or-create (the
    INFERTILITY_TREATMENT catalog schema is shared/seeded, so this rarely creates it)."""
    schema = (
        await session.execute(
            select(FormSchema).where(
                FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value
            )
        )
    ).scalar_one_or_none()
    if schema is None:
        schema = FormSchema(
            id=uuid7(),
            insurance_type=InsuranceType.INFERTILITY_TREATMENT.value,
            name="Analytics Test Schema",
        )
        session.add(schema)
        await session.flush()
        schema_version_id = uuid7()
        session.add(
            SchemaVersion(id=schema_version_id, schema_id=schema.id, version=1, schema_json={})
        )
    else:
        schema_version_id = (
            await session.execute(
                select(SchemaVersion.id)
                .where(SchemaVersion.schema_id == schema.id)
                .order_by(SchemaVersion.version.desc())
                .limit(1)
            )
        ).scalar_one()
    form_id = uuid7()
    session.add(
        PatientForm(
            id=form_id,
            tenant_id=tenant_id,
            schema_version_id=schema_version_id,
            patient_name="Analytics Test Patient",
        )
    )
    await session.flush()
    return form_id


@pytest.mark.asyncio
async def test_queue_status_mirrors_dispatcher_math(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Seed one form per dispatcher-relevant status; the card must count forms in
    the dispatcher's active set as 'active', in_queue forms as 'in_queue', and
    ready_for_processing in neither — exact before/after deltas prove all three."""
    headers = _auth(rbac_world.virtual_assistant_token)
    before = (await client.get(QUEUE_STATUS_PATH, headers=headers)).json()["data"]

    form_ids: list[UUID] = []
    async with admin_sessionmaker() as session, session.begin():
        for status in ("in_queue", "in_call", "ai_processing", "ready_for_processing"):
            form_id = await _seed_form(session, rbac_world.tenant_id)
            form_ids.append(form_id)
            await session.execute(
                update(PatientForm).where(PatientForm.id == form_id).values(status=status)
            )
        limit = (
            await session.execute(
                select(Tenant.max_concurrent_calls).where(Tenant.id == rbac_world.tenant_id)
            )
        ).scalar_one()

    try:
        resp = await client.get(QUEUE_STATUS_PATH, headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.headers["cache-control"] == "no-store"
        data = resp.json()["data"]
        assert data["limit"] == limit
        assert data["active"] == before["active"] + 2  # in_call + ai_processing only
        assert data["in_queue"] == before["in_queue"] + 1  # ready_for_processing counts nowhere
    finally:
        await _delete_forms(admin_sessionmaker, *form_ids)


@pytest.mark.asyncio
async def test_queue_status_denied_without_calls_read(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(QUEUE_STATUS_PATH, headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_queue_status_is_tenant_isolated(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A queued form in ANOTHER tenant never shows in this tenant's counts."""
    before = (await client.get(QUEUE_STATUS_PATH, headers=_auth(rbac_world.admin_token))).json()[
        "data"
    ]
    async with admin_sessionmaker() as session, session.begin():
        other_form = await _seed_form(session, rbac_world.other_tenant_id)
        await session.execute(
            update(PatientForm).where(PatientForm.id == other_form).values(status="in_queue")
        )
    try:
        after = (await client.get(QUEUE_STATUS_PATH, headers=_auth(rbac_world.admin_token))).json()[
            "data"
        ]
        assert after == before
    finally:
        await _delete_forms(admin_sessionmaker, other_form)


@pytest.mark.asyncio
async def test_live_panel_matches_live_monitoring(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The ticket's hard rule: sum(active) == /calls/stats live == len(GET /calls)."""
    headers = _auth(rbac_world.supervisor_token)
    # Supervisor visibility: own and published calls count, another user's unpublished one must not.
    async with admin_sessionmaker() as session, session.begin():
        own_form = await _seed_form(session, rbac_world.tenant_id)
        published_form = await _seed_form(session, rbac_world.tenant_id)
        hidden_form = await _seed_form(session, rbac_world.tenant_id)
        queued_form = await _seed_form(session, rbac_world.tenant_id)
        await session.execute(
            update(PatientForm).where(PatientForm.id == queued_form).values(status="in_queue")
        )
    await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        own_form,
        initiated_by_id=rbac_world.supervisor_id,
        status="active",
    )
    await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        published_form,
        initiated_by_id=rbac_world.admin_id,
        status="active",
        published=True,
    )
    await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        hidden_form,
        initiated_by_id=rbac_world.admin_id,
        status="active",
    )

    try:
        panel = (await client.get(LIVE_PATH, headers=headers)).json()["data"]
        stats = (await client.get("/api/v1/calls/stats", headers=headers)).json()["data"]
        live_list = (await client.get("/api/v1/calls", headers=headers)).json()["data"]

        assert sum(r["active"] for r in panel["rows"]) == stats["live"] == len(live_list)
        assert sum(r["in_queue"] for r in panel["rows"]) >= 1
    finally:
        await _delete_calls_and_forms(
            admin_sessionmaker, own_form, published_form, hidden_form, queued_form
        )


@pytest.mark.asyncio
async def test_unmatched_provider_text_lands_in_the_null_bucket(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        form_id = await _seed_form(session, rbac_world.tenant_id)
        await session.execute(
            update(PatientForm)
            .where(PatientForm.id == form_id)
            .values(status="in_queue", insurance_provider="No Such Payer Inc")
        )

    try:
        resp = await client.get(LIVE_PATH, headers=_auth(rbac_world.admin_token))
        rows = resp.json()["data"]["rows"]
        bucket = next(r for r in rows if r["provider_id"] is None)
        assert bucket["provider_name"] is None
        assert bucket["in_queue"] >= 1
    finally:
        await _delete_forms(admin_sessionmaker, form_id)


@pytest.mark.asyncio
async def test_live_panel_requires_reports_dashboard(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    # VIRTUAL_ASSISTANT holds calls:read but NOT reports:dashboard.
    resp = await client.get(LIVE_PATH, headers=_auth(rbac_world.virtual_assistant_token))
    assert resp.status_code == 403
    ok_resp = await client.get(LIVE_PATH, headers=_auth(rbac_world.supervisor_token))
    assert ok_resp.status_code == 200


async def _seed_report_world(
    admin_sessionmaker: async_sessionmaker[AsyncSession], world: RBACWorld
) -> tuple[list[UUID], list[UUID]]:
    """5 calls in the window (2 completed w/ known durations+completion, 1 active,
    1 canceled, 1 no-answer that never connected; the second completed call lands a
    day later), whisper+coach interventions on the first call and a whisper on the
    second, 1 call in the previous window.
    Returns (call_ids, form_ids) for the caller to clean up."""
    specs = [
        # (status, connected, ended offset minutes, completion_pct, day offset)
        ("completed", True, 5, "80.00", 0),
        ("completed", True, 10, "60.00", 1),
        ("active", True, None, "0", 0),
        ("canceled", True, 3, "20.00", 0),
        # Dead dial: terminal with a frozen form completion but no started_at —
        # must count in volume yet stay out of the completion average.
        ("no_answer", False, None, "50.00", 0),
    ]
    call_ids: list[UUID] = []
    form_ids: list[UUID] = []
    for status, connected, end_min, completion, day_off in specs:
        async with admin_sessionmaker() as session, session.begin():
            form_id = await _seed_form(session, world.tenant_id)
        form_ids.append(form_id)
        call_id = await seed_call(
            admin_sessionmaker,
            world.tenant_id,
            form_id,
            initiated_by_id=world.supervisor_id,
            status=status,
        )
        call_ids.append(call_id)
        started = _FROM.replace(hour=12) + timedelta(days=day_off)
        values: dict[str, object] = {
            "created_at": started,
            # seed_call stamps started_at; a dead dial must clear it explicitly.
            "started_at": started if connected else None,
            "completion_pct": Decimal(completion),
        }
        if end_min is not None:
            values["ended_at"] = started + timedelta(minutes=end_min)
        async with admin_sessionmaker() as session, session.begin():
            await session.execute(update(Call).where(Call.id == call_id).values(**values))

    async with admin_sessionmaker() as session, session.begin():
        session.add_all(
            InterventionEvent(
                tenant_id=world.tenant_id,
                call_id=call_ids[idx],
                supervisor_id=world.supervisor_id,
                type=itype,
            )
            for idx, itype in ((0, "whisper"), (0, "coach"), (1, "whisper"))
        )

    # One call in the PREVIOUS window.
    async with admin_sessionmaker() as session, session.begin():
        prev_form = await _seed_form(session, world.tenant_id)
    form_ids.append(prev_form)
    prev_call = await seed_call(
        admin_sessionmaker,
        world.tenant_id,
        prev_form,
        initiated_by_id=world.supervisor_id,
        status="completed",
    )
    call_ids.append(prev_call)
    async with admin_sessionmaker() as session, session.begin():
        await session.execute(update(Call).where(Call.id == prev_call).values(created_at=_PREV))

    return call_ids, form_ids


@pytest.mark.asyncio
async def test_report_matches_hand_computed_numbers(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _call_ids, form_ids = await _seed_report_world(admin_sessionmaker, rbac_world)
    try:
        resp = await client.get(
            REPORT_PATH,
            params={"date_from": _FROM.isoformat(), "date_to": _TO.isoformat()},
            headers=_auth(rbac_world.admin_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["cache-control"] == "no-store"
        data = resp.json()["data"]

        cur = data["current"]
        assert cur["call_volume"] == 5
        # Durations: 300s, 600s, 3*60=180s over the three ended calls -> avg 360s.
        assert cur["avg_duration_seconds"] == pytest.approx(360.0)
        # Completion over CONNECTED terminal calls only: (80 + 60 + 20) / 3 —
        # the no-answer's frozen 50 must not enter the average.
        assert cur["avg_completion_pct"] == pytest.approx(160 / 3)
        assert cur["intervened_calls"] == 2
        assert cur["intervention_rate"] == pytest.approx(0.4)

        assert data["previous"]["call_volume"] == 1

        days = {row["day"]: row["calls"] for row in data["calls_per_day"]}
        assert days == {"2026-01-08": 4, "2026-01-09": 1}
        assert data["interventions_by_type"] == [
            {"type": "coach", "count": 1},
            {"type": "whisper", "count": 2},
        ]
        assert data["interventions_per_day"] == [
            {"day": "2026-01-08", "flag": 0, "coach": 1, "whisper": 1, "takeover": 0},
            {"day": "2026-01-09", "flag": 0, "coach": 0, "whisper": 1, "takeover": 0},
        ]
    finally:
        await _delete_calls_and_forms(admin_sessionmaker, *form_ids)


@pytest.mark.asyncio
async def test_report_filters_narrow_by_va(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """The virtual assistant never initiates calls in this fixed window, so filtering
    by their id must report zero volume regardless of what else the window holds."""
    resp = await client.get(
        REPORT_PATH,
        params={
            "date_from": _FROM.isoformat(),
            "date_to": _TO.isoformat(),
            "va_id": str(rbac_world.virtual_assistant_id),
        },
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["current"]["call_volume"] == 0


@pytest.mark.asyncio
async def test_report_rejects_inverted_range(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(
        REPORT_PATH,
        params={"date_from": _TO.isoformat(), "date_to": _FROM.isoformat()},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_report_rejects_range_over_366_days(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(
        REPORT_PATH,
        params={
            "date_from": _FROM.isoformat(),
            "date_to": (_FROM + timedelta(days=367)).isoformat(),
        },
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_report_requires_reports_dashboard(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(
        REPORT_PATH,
        params={"date_from": _FROM.isoformat(), "date_to": _TO.isoformat()},
        headers=_auth(rbac_world.virtual_assistant_token),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_filters_lists_active_providers_and_call_owners(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Seeds its own call so the VA-owner assertion never depends on other tests'
    cleanup order."""
    async with admin_sessionmaker() as session, session.begin():
        form_id = await _seed_form(session, rbac_world.tenant_id)
    await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        form_id,
        initiated_by_id=rbac_world.supervisor_id,
        status="active",
    )
    try:
        resp = await client.get(FILTERS_PATH, headers=_auth(rbac_world.admin_token))
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        data = resp.json()["data"]
        assert data["providers"] == [] or {"id", "name"} == set(data["providers"][0])
        assert any(v["id"] == str(rbac_world.supervisor_id) for v in data["vas"])
    finally:
        await _delete_calls_and_forms(admin_sessionmaker, form_id)


@pytest.mark.asyncio
async def test_filters_requires_reports_dashboard(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(FILTERS_PATH, headers=_auth(rbac_world.virtual_assistant_token))
    assert resp.status_code == 403
