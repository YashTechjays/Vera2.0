"""Probe the real intake HTTP endpoint (POST /api/v1/patient-forms) against a
running local API server, with payloads derived from the Google Sheet's actual
field set (data/ibv_infertility_appscript.js's DATA_MAPPING) — not a synthetic
"fill every leaf" payload, but exactly the section/field shape the real Sheet
submits. Catches drift between the Sheet's field set and the live schema (a
stale/renamed/moved path in DATA_MAPPING shows up as a 422 "unknown field paths"
here, the same way it would for a real submission), and makes concrete exactly
which fields the endpoint actually requires (only `system_fields` targets — see
`intake.py::missing_required`).

    just api                                    # in one terminal: start the server
    uv run python scripts/intake_scenarios.py   # in another: run every scenario

Scenarios:
    full              every field the Sheet has a cell for, filled in — expect 200.
    system_only       only the schema's `system_fields` targets — expect 200 (per
                      `missing_required`, that's the only requiredness signal at intake).
    drop_system_field for each `system_fields` target in turn: the full payload with
                      that one field removed — expect a 422 naming exactly that path.

Requires the baseline schemas already seeded (`just seed` / `just seed-schemas`) and
`node` on PATH (used to read DATA_MAPPING out of the real appscript source, so there
is no second, hand-maintained copy of the field list to drift). Mints its own
short-lived `intake:write` API key directly in the DB (bypassing the human-session
`POST /api-keys` flow, since this is a local dev tool) and revokes it on exit, and
cleans up the `patient_form` rows it creates before each run (fixed chart_number
markers, same idempotency convention as `seed_patient_data.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import functools
import json
import re
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.api_key import format_token, hash_secret, new_salt, new_secret
from vera_core.config import get_settings
from vera_core.db import create_engine, create_sessionmaker
from vera_core.models import ApiKey, FormSchema, PatientForm, SchemaVersion, Tenant
from vera_core.models.enums import InsuranceType, VersionStatus

_DIGIT_PATTERN = re.compile(r"^\^\[0-9\]\{(\d+)\}\$$")

# Same tenant `just seed` provisions — kept as a local constant rather than a
# cross-script import so this file still runs standalone (see seed_patient_data.py's
# identical note on _TENANT_SLUG).
_TENANT_SLUG = "vera-health-example"
_PROBE_KEY_NAME = "intake-scenarios-probe"
_MARKER_CHART_NUMBER = {
    "full": "PROBE-FULL-SCENARIO",
    "system_only": "PROBE-SYSTEM-ONLY-SCENARIO",
}
_APPSCRIPT_PATH = Path(__file__).resolve().parents[1] / "data" / "ibv_infertility_appscript.js"


def _dummy_leaf_value(field_key: str, leaf: dict[str, Any]) -> str:
    """A schema-appropriate placeholder for one leaf — identical generator to
    seed_patient_data.py's (duplicated rather than cross-imported, same reason:
    this script must run standalone with no package context on sys.path)."""
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
        "phone": "+1 555 0100",
    }
    leaf_type = leaf.get("type")
    if isinstance(leaf_type, str) and leaf_type in placeholders:
        return placeholders[leaf_type]
    return f"Sample {leaf.get('title') or field_key}"


def _load_sheet_field_skeleton() -> dict[str, Any]:
    """The exact section/field tree the Sheet submits, read live from the appscript
    source's DATA_MAPPING (never hand-duplicated). Cell references are irrelevant here —
    every leaf collapses to `True`, a marker meaning "the Sheet has a cell for this
    field"; the actual value comes from the live schema in `_fill_from_skeleton`, not
    the cell.

    Extracted via a temp CommonJS module (`require`), not `eval` — the appscript source
    is a trusted, repo-local file, but `require`-as-a-module keeps this from ever being able
    to execute arbitrary injected script text, only a plain object literal assignment."""
    src = _APPSCRIPT_PATH.read_text()
    match = re.search(r"const DATA_MAPPING = (\{[\s\S]*?\n\});", src)
    if match is None:
        raise RuntimeError(f"DATA_MAPPING not found in {_APPSCRIPT_PATH}")
    module_src = f"module.exports = {match.group(1)};\n"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as tmp:
        tmp.write(module_src)
        tmp_path = tmp.name
    try:
        node_script = (
            f"const obj = require({json.dumps(tmp_path)});"
            "function strip(node) {"
            "  if (Array.isArray(node)) return true;"
            "  const out = {};"
            "  for (const k in node) out[k] = strip(node[k]);"
            "  return out;"
            "}"
            "process.stdout.write(JSON.stringify(strip(obj)));"
        )
        result = subprocess.run(
            ["node", "-e", node_script], capture_output=True, text=True, check=True
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return json.loads(result.stdout)


def _fill_from_skeleton(
    skeleton: dict[str, Any], schema_fields: dict[str, Any], stale: list[str], prefix: str
) -> dict[str, Any]:
    """Fill every `True` leaf in `skeleton` with a schema-appropriate dummy value,
    walking the matching path in the schema's own `fields` tree. A skeleton key with
    no match in `schema_fields` means the Sheet maps a field this schema version no
    longer has — recorded in `stale` (a real drift signal) rather than raising."""
    out: dict[str, Any] = {}
    for key, value in skeleton.items():
        node = schema_fields.get(key)
        path = f"{prefix}.{key}"
        if node is None:
            stale.append(path)
            continue
        if value is True:
            out[key] = _dummy_leaf_value(key, node)
        else:
            out[key] = _fill_from_skeleton(value, node.get("fields", {}), stale, path)
    return out


def _full_form_payload(
    schema_json: dict[str, Any], sheet_skeleton: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    sections = schema_json["sections"]
    stale: list[str] = []
    payload = {
        section_key: _fill_from_skeleton(
            fields_skeleton, sections[section_key]["fields"], stale, f"sections.{section_key}"
        )
        for section_key, fields_skeleton in sheet_skeleton.items()
        if section_key in sections
    }
    stale.extend(f"sections.{k}.*" for k in sheet_skeleton if k not in sections)
    return payload, stale


def _leaf_dicts_by_path(schema_json: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Root-anchored path -> leaf JSON dict, for every leaf in every section."""
    leaves: dict[str, dict[str, Any]] = {}

    def walk(node: dict[str, Any], prefix: str) -> None:
        fields = node.get("fields")
        if fields is None:
            leaves[prefix] = node
            return
        for key, child in fields.items():
            walk(child, f"{prefix}.{key}")

    for section_key, section in schema_json["sections"].items():
        walk(section, f"sections.{section_key}")
    return leaves


def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
    parts = path.removeprefix("sections.").split(".")
    node = payload
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _delete_path(payload: dict[str, Any], path: str) -> None:
    parts = path.removeprefix("sections.").split(".")
    node = payload
    for part in parts[:-1]:
        node = node.get(part, {})
    node.pop(parts[-1], None)


def _system_fields_only_payload(schema_json: dict[str, Any]) -> dict[str, Any]:
    leaves = _leaf_dicts_by_path(schema_json)
    payload: dict[str, Any] = {}
    for path in (schema_json.get("system_fields") or {}).values():
        leaf = leaves.get(path)
        if leaf is None:
            continue
        _set_path(payload, path, _dummy_leaf_value(path.rsplit(".", 1)[-1], leaf))
    return payload


def _drop_system_field_payloads(
    schema_json: dict[str, Any], full_payload: dict[str, Any]
) -> list[tuple[str, str, dict[str, Any]]]:
    """(system_fields handle, path, full payload minus that one field) per handle."""
    out = []
    for handle, path in (schema_json.get("system_fields") or {}).items():
        variant = copy.deepcopy(full_payload)
        _delete_path(variant, path)
        out.append((handle, path, variant))
    return out


@asynccontextmanager
async def _db_session() -> AsyncIterator[AsyncSession]:
    """Same privileged-role, RLS-bypassing session as seed.py/seed_patient_data.py."""
    engine = create_engine(get_settings())
    try:
        async with create_sessionmaker(engine)() as session, session.begin():
            yield session
    finally:
        await engine.dispose()


async def _resolve_published_schema(
    session: AsyncSession,
) -> tuple[UUID, UUID, int, dict[str, Any]]:
    version, form_schema = (
        await session.execute(
            select(SchemaVersion, FormSchema)
            .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
            .where(
                FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value,
                SchemaVersion.status == VersionStatus.PUBLISHED.value,
            )
        )
    ).one()
    return form_schema.id, version.id, version.version, version.schema_json


async def _mint_probe_key(session: AsyncSession, tenant_id: UUID) -> str:
    """Replace any prior probe key for this tenant (idempotent), mint a fresh
    intake:write key, and commit — a separate process (the running API server)
    must see this row, so the caller's `session.begin()` block commits on exit."""
    await session.execute(
        delete(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.name == _PROBE_KEY_NAME)
    )
    await session.flush()
    salt = new_salt()
    secret = new_secret()
    key = ApiKey(
        tenant_id=tenant_id,
        name=_PROBE_KEY_NAME,
        salt=salt,
        key_hash=hash_secret(salt, secret),
        scope="intake:write",
    )
    session.add(key)
    await session.flush()
    return format_token(tenant_id, key.id, secret)


async def _revoke_probe_key(tenant_id: UUID) -> None:
    async with _db_session() as session:
        await session.execute(
            delete(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.name == _PROBE_KEY_NAME)
        )


def _post_intake(
    base_url: str, token: str, form_type_id: UUID, schema_version_id: UUID, payload: dict[str, Any]
) -> httpx.Response:
    return httpx.post(
        f"{base_url}/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(schema_version_id),
            "intake_payload": payload,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def _report(label: str, resp: httpx.Response) -> None:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    message = body.get("message", "<no message>")
    data = body.get("data")
    extra = f" data={data}" if data else ""
    print(f"[{label}] HTTP {resp.status_code} — {message}{extra}")


async def run(base_url: str, only: str | None) -> None:
    async with _db_session() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.slug == _TENANT_SLUG))
        ).scalar_one()
        form_type_id, schema_version_id, version_no, schema_json = await _resolve_published_schema(
            session
        )
        token = await _mint_probe_key(session, tenant_id)
        # Idempotency: clear any patient_form rows this script previously created for
        # the scenarios that are expected to succeed (no dedup on the real endpoint).
        await session.execute(
            delete(PatientForm).where(
                PatientForm.chart_number.in_(list(_MARKER_CHART_NUMBER.values()))
            )
        )

    print(f"Using infertility_treatment schema v{version_no} ({schema_version_id})")

    sheet_skeleton = _load_sheet_field_skeleton()
    full_payload, stale = _full_form_payload(schema_json, sheet_skeleton)
    if stale:
        print("WARNING: appscript DATA_MAPPING references paths not in the live schema:")
        for path in stale:
            print(f"  - {path}")

    # form_type / schema_version / auth are fixed for the whole run; bind them once.
    post = functools.partial(_post_intake, base_url, token, form_type_id, schema_version_id)
    try:
        if only in (None, "full"):
            payload = copy.deepcopy(full_payload)
            _set_path(
                payload, "sections.patient_information.chart_number", _MARKER_CHART_NUMBER["full"]
            )
            _report("full", post(payload))

        if only in (None, "system_only"):
            payload = _system_fields_only_payload(schema_json)
            _set_path(
                payload,
                "sections.patient_information.chart_number",
                _MARKER_CHART_NUMBER["system_only"],
            )
            _report("system_only", post(payload))

        if only in (None, "drop_system_field"):
            for handle, path, variant in _drop_system_field_payloads(schema_json, full_payload):
                _report(f"drop:{handle} ({path})", post(variant))
    finally:
        await _revoke_probe_key(tenant_id)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--scenario",
        choices=["all", "full", "system_only", "drop_system_field"],
        default="all",
        help="which scenario to run (default: all three)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(run(args.base_url, None if args.scenario == "all" else args.scenario))
