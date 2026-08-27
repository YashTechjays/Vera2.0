"""Seed (and arm) one synthetic infertility `patient_form` in the state a FOCUSED
RETRY dispatches from — a second call that should ask only the still-missing fields.

    just seed-retry-form                                   # seed the form
    just seed-retry-form "--missing deductibles,out_of_pocket"
    just arm-retry-form                                    # arm it: → IN_QUEUE as a retry

The seeded form carries one prior COMPLETED call that captured the rep name AND the
call reference number, judge-supported `ai_call` answers for every collect section
except the ones left missing, one judge-REJECTED answer (so a filled section still
owes a field), and `retry_count = 1`. What the dispatcher's retry branch actually
reads is the current answer at the schema's `rep_call_reference_number_field`: with
one on file, the plan is narrowed to what no authoritative call confirmed, and
staging that narrowed plan is what selects `CallMode.RETRY` — `retry_count` plays no
part in the mode choice.

`arm` transitions EXCEPTION_REVIEW → IN_QUEUE with `manual=False` so `retry_count`
survives the requeue unreset (the operator endpoint's `manual=True` would zero it) —
it no longer picks the call's mode, but it still gates the tenant's auto-retry
budget. Arming here is the only way to exercise the focused-retry path without
waiting on the auto-retry kill-switch.

Idempotent: keyed to a fixed `chart_number` marker, so re-running replaces the
previously-seeded form. Requires the baseline schemas (`just seed`).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.config import get_settings
from vera_core.db import create_engine, create_sessionmaker
from vera_core.forms.conditions import is_applicable, leaf_gates
from vera_core.forms.dsl import COLLECTED_ROLES, FormSchemaDoc, Leaf
from vera_core.forms.intake import (
    iter_leaf_answers,
    missing_required,
    promote_columns,
    resolve_path,
)
from vera_core.forms.review import (
    FieldStatus,
    focus_paths,
    satisfied_required_fraction,
)
from vera_core.models import (
    AppUser,
    Call,
    CallFormSnapshot,
    ExportArtifact,
    FieldAnswer,
    FieldEvaluation,
    FormSchema,
    PatientForm,
    SchemaVersion,
    Tenant,
)
from vera_core.models.enums import (
    AnswerSource,
    CallMode,
    CallStatus,
    FormStatus,
    InsuranceType,
    ReviewReason,
    VersionStatus,
)
from vera_core.services.field_answers import current_values_by_path, recompute_form_projection
from vera_core.services.field_status import load_authoritative_call_ids, load_field_status
from vera_core.services.form_state_machine import FormStateMachine

_TENANT_SLUG = os.environ.get("SEED_TENANT_SLUG", "vera-health-example")
_MARKER_CHART_NUMBER = "TEST-SEED-RETRY"

# Sections the first call never reached — the retry's whole job.
_DEFAULT_MISSING = ("deductibles", "out_of_pocket", "lifetime_maximum")

# Judge-REJECTED answers inside an otherwise-complete section, so the retry set spans a
# filled group too and `expand_to_groups` grows it to the whole panel. Both members of
# the cost-share either/or pair have to go: one answer satisfies the other, so rejecting
# only the copay leaves the pair satisfied by its coinsurance sibling.
_REJECTED_PATHS = (
    "sections.diagnostic_testing.labs_xray_ultrasound.cpt_58340.copay",
    "sections.diagnostic_testing.labs_xray_ultrasound.cpt_58340.coinsurance",
)

_CAPTURE_CONFIDENCE = 90  # extractor's self-reported score on the first call
_JUDGE_CONFIDENCE = 94  # post-call judge verdict (>= the review floor)
_REJECTED_CONFIDENCE = 38  # the judge's score on the field it would not support

# `retry_fill_threshold` is what decides retry-vs-park. Left at the seed default (0.5)
# a form this full reads as "good enough" and would never be retried, so the scenario
# needs a tenant that wants near-complete forms.
_FILL_THRESHOLD = 0.95

_DIGIT_PATTERN = re.compile(r"^\^\[0-9\]\{(\d+)\}\$$")

# What the intake sheet carries: the context sections plus the policy number the
# payer reads back. Every collect answer below comes from the call, not from here.
_INTAKE_PAYLOAD: dict[str, dict[str, str]] = {
    "patient_information": {
        "chart_number": _MARKER_CHART_NUMBER,
        "patient_name": "Dana Whitfield",
        "patient_dob": "3/14/1990",
        "patient_gender": "Female",
    },
    "appointment_information": {
        "appointment_type": "New Patient",
        "appointment_date": "9/02/2026",
    },
    "verification_information": {
        "verified_by": "Dr. Reyes",
        "callback_number": "+15125550188",
    },
    "hospital_information": {
        "hospital_name": "Demo Health Partners",
        "hospital_address": "123 Demo St, Austin, TX",
        "tax_id": "987654313",
        "npi": "1234567893",
    },
    "provider_reference_information": {
        "provider_name": "Dr. Jane Smith",
        "npi": "1982736450",
        "office_location": "Austin Fertility Center",
    },
    "insurance_reference_information": {
        "insurance_provider_name": "Demo Health Plan",
        "insurance_phone_number": "+18005550142",
    },
    "insurance_information": {
        "policy_number": "POL-661522",
    },
}

# What the first call put on the record for its own wrap-up task. The reference number
# is the focus gate — without it the dispatcher retries FRESH instead of FOCUSED.
_FIRST_CALL_REP_NAME = "Marcus Webb"
_FIRST_CALL_REFERENCE = "8842-QX-77"


def _answer_value(leaf: Leaf) -> str:
    """A schema-shaped stand-in for what the rep said: the first enum option, a value
    matching a fixed-length digit pattern, else a type-appropriate placeholder."""
    if leaf.type == "enum" and leaf.values:
        return str(leaf.values[0])
    pattern = leaf.validation.pattern if leaf.validation else None
    if pattern and (match := _DIGIT_PATTERN.match(pattern)):
        length = int(match.group(1))
        return ("1" + "0" * (length - 1))[:length]
    placeholders = {
        "date": "6/15/2026",
        "currency": "$25",
        "percent": "20%",
        "integer": "2",
        "phone": "+18005550142",
    }
    return placeholders.get(leaf.type, f"Sample {leaf.title}")


def _call_answers(doc: FormSchemaDoc, missing: Iterable[str]) -> dict[str, str]:
    """`{field_path: value}` for every collectable leaf the first call filled.

    Walked in document order so a gate is decided before the leaves it governs, and
    only applicable leaves get an answer — an answer under a closed gate would show a
    value in a section the form treats as not applicable.
    """
    skipped = set(missing)
    shared = doc.shared_conditions or {}
    answers: dict[str, Any] = dict(_intake_values())
    filled: dict[str, str] = {}
    for path, leaf, gates in leaf_gates(doc):
        if path.split(".")[1] in skipped or leaf.role not in COLLECTED_ROLES:
            continue
        if not is_applicable(gates, answers, shared):
            continue
        # A leaf intake already carries is a read-back: the rep confirms the value on
        # file rather than volunteering a new one.
        filled[path] = answers[path] = answers.get(path) or _answer_value(leaf)
    filled[doc.rep_call_reference_number_field] = _FIRST_CALL_REFERENCE
    if (rep_name := _rep_name_path(doc)) is not None:
        filled[rep_name] = _FIRST_CALL_REP_NAME
    return filled


def _rep_name_path(doc: FormSchemaDoc) -> str | None:
    """The rep-name leaf beside the reference number, so the wrap-up reads as captured."""
    section = doc.rep_call_reference_number_field.rsplit(".", 1)[0]
    return next(
        (
            path
            for path, _leaf, _gates in leaf_gates(doc)
            if path.startswith(f"{section}.") and path.endswith(".rep_name")
        ),
        None,
    )


def _intake_values() -> dict[str, Any]:
    """The intake payload as root-anchored `{field_path: value}`."""
    return dict(iter_leaf_answers({"sections": _INTAKE_PAYLOAD}))


@asynccontextmanager
async def _seeding_session() -> AsyncIterator[AsyncSession]:
    """A transactional session for a seed run, with the engine disposed on exit.
    Runs as the privileged DB user (bypasses RLS) — see `scripts/seed.py`."""
    engine = create_engine(get_settings())
    try:
        async with create_sessionmaker(engine)() as session, session.begin():
            yield session
    finally:
        await engine.dispose()


async def _published_infertility_version(session: AsyncSession) -> SchemaVersion:
    return (
        await session.execute(
            select(SchemaVersion)
            .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
            .where(
                FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value,
                SchemaVersion.status == VersionStatus.PUBLISHED.value,
            )
        )
    ).scalar_one()


async def _delete_previous(session: AsyncSession) -> None:
    """Drop the previously-seeded form. `patient_form`'s ON DELETE RESTRICT children
    block the delete and must go first; field_answer/field_evaluation cascade."""
    superseded = select(PatientForm.id).where(PatientForm.chart_number == _MARKER_CHART_NUMBER)
    for child in (Call, ExportArtifact):
        await session.execute(delete(child).where(child.form_id.in_(superseded)))
    await session.execute(
        delete(PatientForm).where(PatientForm.chart_number == _MARKER_CHART_NUMBER)
    )


def _report(
    doc: FormSchemaDoc,
    status_by_path: Mapping[str, FieldStatus],
    values: Mapping[str, Any],
    schema_json: Mapping[str, Any],
    floor: int,
    authoritative: frozenset[UUID],
) -> None:
    """Print the scope a correct focused retry would ask — the yardstick for the call."""
    focus = focus_paths(
        doc,
        status_by_path,
        schema_json,
        floor=floor,
        values=values,
        authoritative_calls=authoritative,
    )
    # The dispatcher's gate is exactly "some call captured a reference", i.e. a non-empty
    # authoritative set — never the CURRENT answer at the reference path, which a reviewer's
    # edit supersedes with a human row.
    print(f"  focus gate (a call captured one) : {bool(authoritative)}")
    print(f"  authoritative calls on file      : {len(authoritative)}")
    print(f"  focused ask set (focus_paths)    : {len(focus)}")
    sections = sorted({path.split(".")[1] for path in focus})
    print(f"  sections a focused retry covers: {', '.join(sections)}")


async def seed(missing: tuple[str, ...]) -> None:
    settings = get_settings()
    floor = settings.post_call_review_floor
    async with _seeding_session() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == _TENANT_SLUG))
        ).scalar_one()
        version = await _published_infertility_version(session)
        schema_json = version.schema_json
        doc = FormSchemaDoc.model_validate(schema_json)

        if unknown := sorted(set(missing) - set(doc.sections)):
            raise SystemExit(f"unknown section(s): {', '.join(unknown)}")
        if gaps := missing_required(_INTAKE_PAYLOAD, schema_json):
            raise SystemExit(f"intake payload is missing required fields: {gaps}")

        await _delete_previous(session)
        tenant.retry_fill_threshold = _FILL_THRESHOLD

        # Nested lookup, so the payload goes in section-rooted (resolve_path strips the
        # `sections.` prefix itself). Also validates the hand-written dates.
        promoted = promote_columns(lambda p: resolve_path(_INTAKE_PAYLOAD, p), doc)
        form = PatientForm(
            tenant_id=tenant.id,
            schema_version_id=version.id,
            status=FormStatus.EXCEPTION_REVIEW.value,
            review_reason=ReviewReason.AUTO_RETRY_DISABLED.value,
            intake_payload=_INTAKE_PAYLOAD,
            patient_name=promoted.patient_name,
            patient_dob=promoted.patient_dob,
            appointment_date=promoted.appointment_date,
            chart_number=promoted.chart_number,
            appointment_type=promoted.appointment_type,
            member_id=promoted.member_id,
            insurance_provider=promoted.insurance_provider,
            insurance_provider_phone_number=promoted.insurance_provider_phone_number,
            # One retry already charged, so the next dispatch takes the RETRY branch.
            retry_count=1,
        )
        session.add(form)
        await session.flush()

        ended = datetime.now(UTC) - timedelta(hours=2)
        call = Call(
            tenant_id=tenant.id,
            form_id=form.id,
            mode=CallMode.FULL.value,
            current_status=CallStatus.COMPLETED.value,
            rep_info={"name": _FIRST_CALL_REP_NAME},
            started_at=ended - timedelta(minutes=21),
            ended_at=ended,
        )
        session.add(call)
        await session.flush()

        call_answers = _call_answers(doc, missing)
        answers = [
            FieldAnswer(
                tenant_id=tenant.id,
                form_id=form.id,
                field_path=path,
                value={"value": raw},
                source=AnswerSource.INTAKE.value,
                # The call's answer supersedes intake on the one path they share,
                # exactly as an agreeing `ai_call` answer does in production.
                is_current=path not in call_answers,
            )
            for path, raw in _intake_values().items()
        ]
        judged: list[tuple[FieldAnswer, bool, int]] = []
        for path, raw in call_answers.items():
            rejected = path in _REJECTED_PATHS
            answer = FieldAnswer(
                tenant_id=tenant.id,
                form_id=form.id,
                call_id=call.id,
                field_path=path,
                value={"value": raw},
                source=AnswerSource.AI_CALL.value,
                confidence=_CAPTURE_CONFIDENCE,
                evidence="representative confirmed this on the first call",
                is_current=True,
            )
            answers.append(answer)
            judged.append(
                (answer, not rejected, _REJECTED_CONFIDENCE if rejected else _JUDGE_CONFIDENCE)
            )
        session.add_all(answers)
        await session.flush()
        session.add_all(
            FieldEvaluation(
                tenant_id=tenant.id,
                answer_id=answer.id,
                confidence=confidence,
                supported=supported,
                evidence=answer.evidence,
            )
            for answer, supported, confidence in judged
        )
        await session.flush()

        await recompute_form_projection(session, form, schema_json)
        status_by_path = await load_field_status(session, form.id)
        values = await current_values_by_path(session, form.id)
        # Mirrors the real before_state (dispatch, pre-call)/after_state (post_call_eval,
        # post-call) writes so the per-attempt view's changed_paths diff has something to
        # show — without this row the call reads as unfinalized (see CallAttempt.finalized).
        session.add(
            CallFormSnapshot(
                tenant_id=tenant.id,
                call_id=call.id,
                before_state=dict(_intake_values()),
                after_state=dict(values),
            )
        )
        # The seeded call captures the reference number (below), so it is authoritative and
        # `verified_pct` stays close to `completion_pct` rather than collapsing to 0%.
        authoritative = await load_authoritative_call_ids(
            session, form.id, reference_field=doc.rep_call_reference_number_field
        )
        form.verified_pct = round(
            satisfied_required_fraction(
                status_by_path,
                schema_json,
                floor=floor,
                values=values,
                authoritative_calls=authoritative,
            )
            * 100,
            2,
        )
        call.completion_pct = form.completion_pct

        print(f"seeded patient_form {form.id}")
        print(f"  status / retry_count           : {form.status} / {form.retry_count}")
        print(f"  completion / verified          : {form.completion_pct}% / {form.verified_pct}%")
        print(f"  prior call {call.id} reference={_FIRST_CALL_REFERENCE}")
        print(
            f"  answers: {len(answers)} rows, {len(judged)} judged, sections left empty: "
            f"{', '.join(missing)}"
        )
        _report(doc, status_by_path, values, schema_json, floor, authoritative)
        print("\nArm it when you are ready to take the call: just arm-retry-form")


async def arm(actor_email: str | None) -> None:
    """Move the seeded form to IN_QUEUE as a RETRY. See the module docstring for why
    this bypasses the operator endpoint."""
    async with _seeding_session() as session:
        form = (
            await session.execute(
                select(PatientForm)
                .where(PatientForm.chart_number == _MARKER_CHART_NUMBER)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if form is None:
            raise SystemExit("no seeded retry form — run `just seed-retry-form` first")
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == form.tenant_id))
        ).scalar_one()

        # Ownerless by default: `call_authz` shows an ownerless in-flight call to every
        # caller with `calls:read`, so whichever dev login is open can join as the rep.
        owner: UUID | None = None
        if actor_email is not None:
            owner = (
                await session.execute(
                    select(AppUser.id).where(
                        AppUser.tenant_id == form.tenant_id, AppUser.email == actor_email
                    )
                )
            ).scalar_one_or_none()
            if owner is None:
                raise SystemExit(f"no user {actor_email!r} in this tenant")

        FormStateMachine().transition(
            form, FormStatus.IN_QUEUE, tenant_max_retries=tenant.max_retries
        )
        form.enqueued_at = func.now()
        form.enqueued_by_id = owner
        await session.flush()

        print(f"armed patient_form {form.id}: {form.status}, retry_count={form.retry_count}")
        # Mode isn't retry_count-derived — the dispatcher picks RETRY only if it narrows the
        # plan to a captured call reference number's still-unconfirmed fields (queue_dispatcher).
        print("  next dispatch mode (FULL vs RETRY) is decided by the dispatcher at call time")
        print(f"  call owner: {actor_email or 'none (visible to every calls:read login)'}")
        print(
            "  the control plane's pipeline sweeper dispatches within its interval "
            f"({get_settings().pipeline_sweep_interval_seconds}s)"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seed", "arm"), nargs="?", default="seed")
    parser.add_argument(
        "--missing",
        default=",".join(_DEFAULT_MISSING),
        help="comma-separated section keys the first call never reached",
    )
    parser.add_argument("--as-email", default=None, help="arm: the user to attribute the call to")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.command == "arm":
        asyncio.run(arm(args.as_email))
    else:
        sections = tuple(s.strip() for s in args.missing.split(",") if s.strip())
        asyncio.run(seed(sections))
