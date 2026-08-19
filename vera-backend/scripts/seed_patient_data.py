"""Seed (replace) one synthetic infertility `patient_form` for local dev/demo review.

    just test_seed_patient_data                     # ready_for_processing (curated demo fill)
    just test_seed_patient_data exception_review     # full a-to-z fill + a few disputed fields

Or directly: uv run python scripts/seed_patient_data.py --status <status>

Idempotent: each status is keyed to a fixed `chart_number` marker, so re-running
replaces the previously-seeded form for that status instead of piling up rows.
Requires the baseline schemas to already be seeded (`just seed` / `just seed-schemas`).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, NamedTuple

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.config import get_settings
from vera_core.db import create_engine, create_sessionmaker
from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.intake import (
    iter_leaf_answers,
    missing_required,
    promote_columns,
    resolve_path,
)
from vera_core.models import (
    Call,
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
    VersionStatus,
)

_DIGIT_PATTERN = re.compile(r"^\^\[0-9\]\{(\d+)\}\$$")

# Same tenant `just seed` provisions (`scripts/seed.py`'s `SAMPLE_TENANT_SLUG`) — kept as
# a local constant rather than a cross-import so this script still runs standalone
# (`uv run python scripts/seed_patient_data.py`, no package context on `sys.path`).
_TENANT_SLUG = os.environ.get("SEED_TENANT_SLUG", "vera-health-example")

# Curated, realistic values for the fields that matter most for a readable demo — the
# same "prominent overrides on top of a generic fill" convention as the frontend's
# `OVERRIDES` (vera-frontend/src/lib/ibv/mock.ts). Used as-is for READY_FOR_PROCESSING;
# layered on top of `_fill_all_leaves` for EXCEPTION_REVIEW.
_OVERRIDES: dict[str, dict[str, str]] = {
    "patient_information": {
        # Inert: `_build_payload` overwrites this per-status with the idempotency
        # marker (`_MARKER_CHART_NUMBER`); kept only so this demo dict reads complete.
        "chart_number": "CH-10293",
        "patient_name": "Test T",
        "patient_dob": "1991-04-12",
        "patient_gender": "Female",
    },
    "appointment_information": {
        "appointment_type": "New Patient",
        "appointment_date": "2026-08-03",
    },
    "hospital_information": {
        "hospital_name": "Demo Health Partners",
        "hospital_address": "123 Demo St, Austin, TX",
        "tax_id": "987654313",
        "npi": "1234567893",
    },
    "insurance_information": {
        "doctor_inside_network": "Yes",
        "facility_inside_network": "Yes",
        "out_of_network_coverage": "No",
        "plan_type": "PPO",
        "cob_status": "Primary",
        "policy_number": "POL-661522",
        "group_number": "GRP-3140",
        "group_name": "Umbrella Health",
        "policy_situs": "TX",
    },
    "benefit_coverage": {
        "benefit_year_type": "Calendar Year",
        "plan_effective_date": "01/01/2026",
        "plan_year_information": "01/01/2026 - 12/31/2026",
        "coverage_type": "Family",
        "pcp_referral_required": "No",
        "telehealth_covered": "Yes",
        "plan_fund_type": "Fully Funded",
        "employer_support_size": "Large Group",
        "infertility_plan_mandate": "Yes",
    },
    "provider_reference_information": {
        "provider_name": "Dr. Jane Smith",
        "npi": "1982736450",
        "office_location": "Austin Fertility Center",
    },
    "insurance_representative": {
        "rep_name": "Taylor Reed",
        "call_reference_number": "REF-88213",
    },
    "insurance_reference_information": {
        "insurance_provider_name": "Demo Health Plan",
        "insurance_phone_number": "+15550100",
    },
    "verification_information": {
        "verified_by": "Dr. Reyes",
        "callback_number": "+15550199",
    },
}


class _Judge(NamedTuple):
    """The post-call judge's verdict, persisted as a `field_evaluation`."""

    confidence: int
    supported: bool


class _Dispute(NamedTuple):
    ai_value: str
    confidence: int
    evidence: str
    judge: _Judge | None = None


# Section-rooted leaf path -> the diverging AI answer: EXCEPTION_REVIEW only. Each becomes
# an INTAKE baseline (the `_OVERRIDES`/generic-fill value, superseded) plus a diverging
# current AI_CALL answer — the dispute signal `is_disputed` reads (`vera_core.forms.review`).
#
# Between them the entries span every state the reviewer's confidence chip renders against
# the frontend's 95/85/75 bands (`vera-frontend/src/lib/ibv/disputes.ts`): judge-supported
# high, judge-supported medium, judge-REJECTED (whose score must not colour the field), and
# no verdict at all (falls back to the capture score), on a plain field row and a matrix cell.
_DISPUTES: dict[str, _Dispute] = {
    "insurance_information.doctor_inside_network": _Dispute(
        "No",
        92,
        "representative said the doctor is out-of-network this year",
        _Judge(100, True),
    ),
    "benefit_coverage.coverage_type": _Dispute(
        "Individual",
        88,
        "representative corrected the policy to individual-only",
        _Judge(92, True),
    ),
    "hospital_information.tax_id": _Dispute(
        "123456789",
        95,
        "representative read back a different tax ID",
        _Judge(95, False),
    ),
    "insurance_information.policy_number": _Dispute(
        "POL-661599",
        85,
        "representative gave a different policy number",
    ),
    # A matrix-cell leaf, so the `ui.layout: "table"` section header exercises the
    # same per-section resolve as a plain field row.
    "diagnostic_testing.labs_xray_ultrasound.cpt_58340.covered": _Dispute(
        "No",
        90,
        "representative said labs are not covered under this plan",
    ),
}

_MARKER_CHART_NUMBER = {
    FormStatus.READY_FOR_PROCESSING: "TEST-SEED-READY",
    FormStatus.EXCEPTION_REVIEW: "TEST-SEED-EXCEPTION",
}


def _dummy_leaf_value(field_key: str, leaf: dict[str, Any]) -> str:
    """A schema-appropriate placeholder for one leaf: first enum option, a value
    matching a fixed-length digit pattern (NPI/tax ID), else a type-shaped placeholder."""
    values = leaf.get("values")
    if leaf.get("type") == "enum" and values:
        return str(values[0])
    pattern = (leaf.get("validation") or {}).get("pattern")
    if pattern:
        match = _DIGIT_PATTERN.match(pattern)
        if match:
            length = int(match.group(1))
            return ("1" + "0" * (length - 1))[:length]
    placeholders: dict[str, str] = {
        "date": "06/15/2026",
        "currency": "$25",
        "percent": "20%",
        "integer": "2",
        "phone": "+15550100",
    }
    leaf_type = leaf.get("type")
    if isinstance(leaf_type, str) and leaf_type in placeholders:
        return placeholders[leaf_type]
    return f"Sample {leaf.get('title') or field_key}"


def _fill_all_leaves(schema_json: dict[str, Any]) -> dict[str, Any]:
    """A-to-z dummy fill: every leaf in every non-`ui_only` section gets a placeholder,
    walking `group` fields to arbitrary depth (same shape as a `Section`'s `fields`)."""

    def walk(node: dict[str, Any], key: str) -> Any:
        if "fields" in node:
            return {
                child_key: walk(child, child_key) for child_key, child in node["fields"].items()
            }
        return _dummy_leaf_value(key, node)

    return {
        section_key: walk(section, section_key)
        for section_key, section in schema_json["sections"].items()
        if section.get("role") != "ui_only"
    }


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _build_payload(schema_json: dict[str, Any], status: FormStatus) -> dict[str, Any]:
    overrides = _deep_merge(
        {}, {**_OVERRIDES, "patient_information": dict(_OVERRIDES["patient_information"])}
    )
    overrides["patient_information"]["chart_number"] = _MARKER_CHART_NUMBER[status]
    if status is FormStatus.READY_FOR_PROCESSING:
        return overrides
    return _deep_merge(_fill_all_leaves(schema_json), overrides)


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


async def seed_patient(status: FormStatus) -> None:
    async with _seeding_session() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.slug == _TENANT_SLUG))
        ).scalar_one()

        version, _form_schema = (
            await session.execute(
                select(SchemaVersion, FormSchema)
                .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
                .where(
                    FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value,
                    SchemaVersion.status == VersionStatus.PUBLISHED.value,
                )
            )
        ).one()
        schema_json = version.schema_json

        payload = _build_payload(schema_json, status)
        missing = missing_required(payload, schema_json)
        if missing:
            raise SystemExit(f"missing required patient_information fields: {missing}")
        doc = FormSchemaDoc.model_validate(schema_json)
        promoted = promote_columns(lambda p: resolve_path(payload, p), doc)

        marker = _MARKER_CHART_NUMBER[status]
        # `patient_form`'s ON DELETE RESTRICT children block the delete and must go first
        # (an exported form leaves an export_artifact); field_answer and its
        # field_evaluation cascade with the form.
        superseded = select(PatientForm.id).where(PatientForm.chart_number == marker)
        for child in (Call, ExportArtifact):
            await session.execute(delete(child).where(child.form_id.in_(superseded)))
        await session.execute(delete(PatientForm).where(PatientForm.chart_number == marker))

        form = PatientForm(
            tenant_id=tenant_id,
            schema_version_id=version.id,
            status=status.value,
            intake_payload=payload,
            patient_name=promoted.patient_name,
            patient_dob=promoted.patient_dob,
            appointment_date=promoted.appointment_date,
            chart_number=promoted.chart_number,
            appointment_type=promoted.appointment_type,
            member_id=promoted.member_id,
            insurance_provider=promoted.insurance_provider,
            insurance_provider_phone_number=promoted.insurance_provider_phone_number,
            completion_pct=0,
            retry_count=0,
        )
        session.add(form)
        await session.flush()

        payload_root = {"sections": payload} if is_v2(schema_json) else payload
        prefix = "sections." if is_v2(schema_json) else ""
        disputes = (
            {f"{prefix}{path}": value for path, value in _DISPUTES.items()}
            if status is FormStatus.EXCEPTION_REVIEW
            else {}
        )

        # `load_field_provenance` joins ai_call answers by call_id, so answers seeded
        # without a call render no provenance and no judge verdict at all. Only enough
        # call for that join — no snapshot or recording, so the attempt timeline's
        # changed-field diff and playback stay empty by design.
        call_id = None
        if disputes:
            call = Call(
                tenant_id=tenant_id,
                form_id=form.id,
                mode=CallMode.FULL.value,
                current_status=CallStatus.COMPLETED.value,
            )
            session.add(call)
            await session.flush()
            call_id = call.id

        rows: list[FieldAnswer] = []
        judged: list[tuple[FieldAnswer, _Judge]] = []
        for path, raw in iter_leaf_answers(payload_root):
            dispute = disputes.get(path)
            rows.append(
                FieldAnswer(
                    tenant_id=tenant_id,
                    form_id=form.id,
                    field_path=path,
                    value={"value": raw},
                    source=AnswerSource.INTAKE.value,
                    is_current=dispute is None,
                )
            )
            if dispute is not None:
                answer = FieldAnswer(
                    tenant_id=tenant_id,
                    form_id=form.id,
                    call_id=call_id,
                    field_path=path,
                    value={"value": dispute.ai_value},
                    source=AnswerSource.AI_CALL.value,
                    confidence=dispute.confidence,
                    evidence=dispute.evidence,
                    is_current=True,
                )
                rows.append(answer)
                if dispute.judge is not None:
                    judged.append((answer, dispute.judge))
        session.add_all(rows)
        await session.flush()

        session.add_all(
            FieldEvaluation(
                tenant_id=tenant_id,
                answer_id=answer.id,
                confidence=judge.confidence,
                supported=judge.supported,
                evidence=answer.evidence,
            )
            for answer, judge in judged
        )

        print(
            f"seeded patient_form {form.id} status={status.value} "
            f"schema_version={version.version} field_answers={len(rows)} "
            f"disputes={len(disputes)} judged={len(judged)}"
        )


def _parse_status(raw: str) -> FormStatus:
    try:
        return FormStatus(raw.strip().lower())
    except ValueError as exc:
        valid = f"{FormStatus.READY_FOR_PROCESSING.value}, {FormStatus.EXCEPTION_REVIEW.value}"
        raise SystemExit(f"unsupported --status {raw!r} (expected one of: {valid})") from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status",
        default=FormStatus.READY_FOR_PROCESSING.value,
        help="ready_for_processing (curated demo fill) or exception_review (full fill + disputes)",
    )
    asyncio.run(seed_patient(_parse_status(parser.parse_args().status)))
