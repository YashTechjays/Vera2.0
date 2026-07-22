# Call Recording & Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every outbound call as audio in GCS via LiveKit composite egress (pending row + sha256 verify), persist tokenized transcripts to Postgres as source of truth, enforce a per-tenant retention policy with audited before/after snapshots, and expose an RBAC-gated playback endpoint minting TTL-bounded signed URLs with an access log.

**Spec:** `docs/superpowers/specs/2026-07-09-call-recording-persistence-design.md` (approved). All settled decisions live there — this plan implements them without re-deciding.

**Architecture:** The control plane starts an audio-only room-composite egress right after `create_call_room()` and inserts a `Recording` row (`PENDING` on success, `FAILED` on start error — fail-open, the call proceeds). A lifespan poller reconciles `PENDING` rows via `ListEgress` + streams the GCS object to compute sha256 (`AVAILABLE`), discarding no-answer/busy recordings. The worker emits a new `call.ended` event at session shutdown; the existing `WorkerEventConsumer` drains the Redis transcript stream into the `transcript` table (idempotent `ON CONFLICT DO NOTHING`). An hourly sweeper deletes recordings past `retention_until` with before/after audit snapshots. Cross-tenant work lists come from `SECURITY DEFINER` functions (the `audit_chain_heads()` precedent); all row processing happens inside `tenant_session(...)`.

**Tech Stack:** FastAPI, SQLAlchemy async, `redis.asyncio` Streams, `livekit-api` (egress), `google-cloud-storage` (sync SDK via `asyncio.to_thread`), Alembic, pytest + mypy --strict + ruff.

## Global Constraints

- Working dir for all commands: `vera-backend/`. Full gate: `just check` (ruff + mypy --strict + pytest).
- Repo rule: after ALL tasks complete, run the `/simplify` skill on the change, then re-run `just check` (Task 12).
- Migrations: create with `just makemigration "<message>"` (random-hex revision id — NEVER hand-number), then replace the generated body. Every column add must be `ADD COLUMN IF NOT EXISTS`; constraints via `DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL; END $$` (migration `0001` runs `create_all` off live models, so fresh CI DBs already have new columns).
- `SECURITY DEFINER` fns: `CREATE OR REPLACE` + `ALTER FUNCTION ... OWNER TO vera_definer_owner`; if a param type ever changes, `DROP FUNCTION` + recreate.
- Timestamps of record come from the DB clock (`func.now()`), never `datetime.now()` — exception: `spoke_at`/`expires_at`-style domain facts derived from event payloads.
- PHI bright lines: no PHI in logs/spans/URLs/paths; transcript stream text is tokenized and is persisted as-is (spec decision 3); GCS object paths are UUIDs only.
- Endpoints: `ResponseModel[T]` via `ok(...)`, `CustomAPIException` (never `HTTPException`), `Cache-Control: no-store` on PHI-adjacent responses, opaque UUIDs in paths.
- asyncio only (no anyio); PEP 695 type params; `redis.asyncio` BLOCK reads raise `TimeoutError` — idle tick, not an error.
- Long-lived loops (verifier, sweeper, consumer handler) MUST be verified by booting the service and watching idle windows (Task 12), not pytest alone.
- Commit after each task; branch: create `feat/call-recording-persistence` off `main` before Task 1.

---

### Task 1: Schema foundation — RecordingStatus, model columns, settings, audit events, migration

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/enums.py` (add `RecordingStatus` after `TranscriptSource`, ~line 65)
- Modify: `packages/vera_core/src/vera_core/models/transcript.py` (Recording columns)
- Modify: `packages/vera_core/src/vera_core/models/tenant.py` (retention knob, after `queue_expiry_hours` ~line 44)
- Modify: `packages/vera_core/src/vera_core/models/audit_log.py` (AuditEvent members, after `QUEUE_EXPIRED` ~line 55)
- Modify: `packages/vera_core/src/vera_core/config/settings.py` (recording settings, after the audit-anchoring block ~line 135)
- Create: migration via `just makemigration "recording lifecycle columns and tenant retention days"`
- Test: `tests/unit/db/test_recording_model.py` (create)

**Interfaces:**
- Consumes: existing `Base`, `check_in`, `TenantColumnMixin`.
- Produces: `RecordingStatus` StrEnum (`PENDING/AVAILABLE/FAILED/DISCARDED/DELETED`, values lowercase); `Recording` columns `status: str`, `egress_id: str | None`, `sha256: str | None`, `size_bytes: int | None`, `duration_ms: int | None`, `deleted_at: datetime | None`; `Tenant.recording_retention_days: int | None`; Settings fields `recording_bucket: str | None`, `recording_prefix: str`, `recording_retention_days_default: int`, `recording_signed_url_ttl_seconds: int`, `recording_verify_interval_seconds: int`, `retention_sweep_interval_seconds: int`; `AuditEvent.RECORDING_START_FAILED/RECORDING_FAILED/RECORDING_DISCARDED/RECORDING_ACCESSED/RECORDING_DELETED`. Later tasks use these exact names.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/db/test_recording_model.py
"""Recording lifecycle columns + catalog values (Task 1 of call-recording plan)."""

from vera_core.models import Recording, Tenant
from vera_core.models.audit_log import AuditEvent
from vera_core.models.enums import RecordingStatus, values_of


def test_recording_status_catalog() -> None:
    assert values_of(RecordingStatus) == (
        "pending",
        "available",
        "failed",
        "discarded",
        "deleted",
    )


def test_recording_lifecycle_columns_exist() -> None:
    cols = Recording.__table__.columns
    assert cols["status"].nullable is False
    for name in ("egress_id", "sha256", "size_bytes", "duration_ms", "deleted_at"):
        assert cols[name].nullable is True


def test_tenant_retention_knob_nullable() -> None:
    assert Tenant.__table__.columns["recording_retention_days"].nullable is True


def test_recording_audit_events_exist() -> None:
    assert AuditEvent.RECORDING_START_FAILED.value == "recording.start_failed"
    assert AuditEvent.RECORDING_FAILED.value == "recording.failed"
    assert AuditEvent.RECORDING_DISCARDED.value == "recording.discarded"
    assert AuditEvent.RECORDING_ACCESSED.value == "recording.accessed"
    assert AuditEvent.RECORDING_DELETED.value == "recording.deleted"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `just test tests/unit/db/test_recording_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'RecordingStatus'`.

- [ ] **Step 3: Implement the model/enum/settings changes**

`enums.py` — insert after `TranscriptSource`:

```python
class RecordingStatus(enum.StrEnum):
    """recording lifecycle. PENDING at egress start; AVAILABLE once the object is
    sha256-verified; FAILED (egress start or run failed); DISCARDED (no-answer/busy
    call — object deleted at verify time); DELETED (retention-sweep tombstone)."""

    PENDING = "pending"
    AVAILABLE = "available"
    FAILED = "failed"
    DISCARDED = "discarded"
    DELETED = "deleted"
```

`transcript.py` — replace the `Recording` class (keep the module docstring; update its last sentence to mention the lifecycle):

```python
class Recording(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    __tablename__ = "recording"
    __table_args__ = (check_in("status", RecordingStatus),)

    call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("call.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gcs_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RecordingStatus.PENDING.value
    )
    egress_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Evidence columns survive deletion (the tombstone keeps proving WHAT was destroyed).
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Add `BigInteger` to the `sqlalchemy` import and `RecordingStatus` to the `vera_core.models.enums` import in that file.

`tenant.py` — after `queue_expiry_hours`:

```python
    # Recording retention in days; NULL → the platform default
    # (settings.recording_retention_days_default). Stamped onto
    # recording.retention_until at verify time; changing it does NOT rewrite
    # already-stamped recordings (spec decision).
    recording_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

`audit_log.py` — append to `AuditEvent`:

```python
    # Recording lifecycle (call audio in GCS). Ids/hashes/sizes only — never audio,
    # never PHI. RECORDING_DELETED is emitted twice per sweep: detail.phase="before"
    # (object snapshot pre-delete) and "after" (verified-gone confirmation).
    RECORDING_START_FAILED = "recording.start_failed"
    RECORDING_FAILED = "recording.failed"
    RECORDING_DISCARDED = "recording.discarded"
    RECORDING_ACCESSED = "recording.accessed"
    RECORDING_DELETED = "recording.deleted"
```

`settings.py` — after the audit-anchoring block:

```python
    # --- call recording (LiveKit composite egress → GCS) --------------------
    # Unset bucket → recording disabled end-to-end (no egress started, no
    # Recording rows, playback 409s) — mirrors the langfuse_host no-op switch.
    recording_bucket: str | None = None  # VERA_RECORDING_BUCKET
    recording_prefix: str = "recordings"  # VERA_RECORDING_PREFIX
    recording_retention_days_default: int = 90  # VERA_RECORDING_RETENTION_DAYS_DEFAULT
    recording_signed_url_ttl_seconds: int = 600  # VERA_RECORDING_SIGNED_URL_TTL_SECONDS
    recording_verify_interval_seconds: int = 30  # VERA_RECORDING_VERIFY_INTERVAL_SECONDS
    retention_sweep_interval_seconds: int = 3600  # VERA_RETENTION_SWEEP_INTERVAL_SECONDS
```

- [ ] **Step 4: Write the migration**

Run: `just makemigration "recording lifecycle columns and tenant retention days"`, then replace the generated `upgrade`/`downgrade` bodies (keep the generated revision ids):

```python
def upgrade() -> None:
    # Idempotent: a fresh DB's 0001 create_all already materialized these columns
    # from the live models; only an already-provisioned DB needs the ADDs.
    op.execute(
        "ALTER TABLE recording ADD COLUMN IF NOT EXISTS status VARCHAR(16) "
        "NOT NULL DEFAULT 'pending'"
    )
    op.execute("ALTER TABLE recording ADD COLUMN IF NOT EXISTS egress_id VARCHAR(128)")
    op.execute("ALTER TABLE recording ADD COLUMN IF NOT EXISTS sha256 VARCHAR(64)")
    op.execute("ALTER TABLE recording ADD COLUMN IF NOT EXISTS size_bytes BIGINT")
    op.execute("ALTER TABLE recording ADD COLUMN IF NOT EXISTS duration_ms BIGINT")
    op.execute("ALTER TABLE recording ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
    op.execute("ALTER TABLE tenant ADD COLUMN IF NOT EXISTS recording_retention_days INTEGER")
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE recording ADD CONSTRAINT ck_recording_status_valid
                CHECK (status IN ('pending','available','failed','discarded','deleted'));
        EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE recording DROP CONSTRAINT IF EXISTS ck_recording_status_valid")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS status")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS egress_id")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS sha256")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS size_bytes")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS duration_ms")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE tenant DROP COLUMN IF EXISTS recording_retention_days")
```

- [ ] **Step 5: Verify test passes + migration applies**

Run: `just test tests/unit/db/test_recording_model.py -v` — Expected: PASS.
Run: `just up && just migrate` — Expected: `alembic upgrade head` completes without error.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(recording): lifecycle columns, retention knob, settings, audit events"
```

---

### Task 2: Permissions — recordings:read / recordings:manage + seed migration

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/rbac_defaults.py`
- Create: migration via `just makemigration "seed recordings permissions"`
- Test: `tests/unit/test_rbac_defaults.py` (extend existing)

**Interfaces:**
- Produces: permission codes `recordings:read` (TENANT_ADMIN + SUPERVISOR) and `recordings:manage` (TENANT_ADMIN only, via DEFAULT_PERMISSIONS). Tasks 10 & 11 gate on these exact codes.

- [ ] **Step 1: Write the failing test** — add to `tests/unit/test_rbac_defaults.py`:

```python
def test_recordings_permissions_seeded() -> None:
    assert "recordings:read" in DEFAULT_PERMISSIONS
    assert "recordings:manage" in DEFAULT_PERMISSIONS
    assert "recordings:read" in SYSTEM_ROLES["SUPERVISOR"]
    assert "recordings:manage" not in SYSTEM_ROLES["SUPERVISOR"]
    assert "recordings:read" in SYSTEM_ROLES["TENANT_ADMIN"]
    assert "recordings:manage" in SYSTEM_ROLES["TENANT_ADMIN"]
```

(Match the existing import style at the top of that file; add `SYSTEM_ROLES`/`DEFAULT_PERMISSIONS` if not already imported.)

- [ ] **Step 2: Run it to verify it fails** — `just test tests/unit/test_rbac_defaults.py -v` → FAIL.

- [ ] **Step 3: Implement** — in `rbac_defaults.py` add to `DEFAULT_PERMISSIONS` (after `"calls:publish"`):

```python
    "recordings:read": "Play back call recordings (every playback is audited)",
    "recordings:manage": "Manage the tenant's recording retention policy",
```

and add `"recordings:read",` to the `SUPERVISOR` frozenset (TENANT_ADMIN gets both automatically via `frozenset(DEFAULT_PERMISSIONS)`).

- [ ] **Step 4: Seed migration** — `just makemigration "seed recordings permissions"`; body modeled on `20260706_1730_25e54e43fcf3` (runs on the privileged migration connection; `ON CONFLICT DO NOTHING` everywhere):

```python
_PERMS = {
    "recordings:read": "Play back call recordings (every playback is audited)",
    "recordings:manage": "Manage the tenant's recording retention policy",
}
# Which seeded system roles get which new permission.
_GRANTS = {
    "TENANT_ADMIN": ("recordings:read", "recordings:manage"),
    "SUPERVISOR": ("recordings:read",),
    "SUPER_ADMIN": ("recordings:read", "recordings:manage"),
}


def upgrade() -> None:
    for code, description in _PERMS.items():
        op.execute(
            "INSERT INTO permission (id, code, description) "
            f"VALUES (gen_random_uuid(), '{code}', '{description}') "
            "ON CONFLICT (code) DO NOTHING"
        )
    for role_name, codes in _GRANTS.items():
        for code in codes:
            op.execute(
                "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
                "SELECT gen_random_uuid(), NULL, r.id, p.id "
                "FROM role r, permission p "
                f"WHERE r.tenant_id IS NULL AND r.name = '{role_name}' "
                f"AND p.code = '{code}' "
                "ON CONFLICT (role_id, permission_id) DO NOTHING"
            )


def downgrade() -> None:
    # Same rationale as 25e54e43fcf3: seeded grants are indistinguishable from
    # live product grants added since — never bulk-delete by permission code.
    raise RuntimeError(
        "downgrade unsupported for seed_recordings_permissions — revert by hand "
        "after confirming no live grants exist (see 25e54e43fcf3 for rationale)"
    )
```

- [ ] **Step 5: Verify** — `just test tests/unit/test_rbac_defaults.py -v` → PASS; `just migrate` → applies cleanly.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(rbac): recordings:read + recordings:manage permissions"`

---

### Task 3: LiveKitGateway — start_room_audio_egress + get_egress_status

**Files:**
- Modify: `apps/control_plane/src/control_plane/livekit_gateway.py`
- Test: `tests/unit/control_plane/test_livekit_gateway_egress.py` (create; follow the fake-client pattern of any existing gateway test in `tests/unit/control_plane/`)

**Interfaces:**
- Produces:
  - `class EgressStartError(Exception)` — raised on any egress-start failure; callers fail OPEN.
  - `async def start_room_audio_egress(self, room_name: str, *, bucket: str, object_path: str) -> str` — returns `egress_id`.
  - `class EgressState(NamedTuple)`: `complete: bool; failed: bool; duration_ms: int | None; size_bytes: int | None`.
  - `async def get_egress_status(self, egress_id: str) -> EgressState | None` — `None` when LiveKit no longer knows the egress id. Tasks 5 and 8 consume these exact signatures.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/control_plane/test_livekit_gateway_egress.py
"""Egress wrapper shapes: audio-only room composite → GCS, status mapping."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from livekit import api

from control_plane.livekit_gateway import EgressStartError, LiveKitGateway


class _FakeEgress:
    def __init__(self, start_result: Any = None, list_items: list[Any] | None = None,
                 raise_on_start: Exception | None = None) -> None:
        self.start_result = start_result
        self.list_items = list_items or []
        self.raise_on_start = raise_on_start
        self.start_requests: list[Any] = []

    async def start_room_composite_egress(self, request: Any) -> Any:
        if self.raise_on_start:
            raise self.raise_on_start
        self.start_requests.append(request)
        return self.start_result

    async def list_egress(self, request: Any) -> Any:
        return SimpleNamespace(items=self.list_items)


def _gateway_with(egress: _FakeEgress) -> LiveKitGateway:
    gw = LiveKitGateway(url="ws://test", api_key="k", api_secret="s")

    @asynccontextmanager
    async def _client():  # type: ignore[no-untyped-def]
        yield SimpleNamespace(egress=egress)

    gw._client = _client  # type: ignore[method-assign]
    return gw


async def test_start_room_audio_egress_shapes_request_and_returns_id() -> None:
    egress = _FakeEgress(start_result=SimpleNamespace(egress_id="EG_123"))
    gw = _gateway_with(egress)
    egress_id = await gw.start_room_audio_egress(
        "call--t--c", bucket="vera-recordings", object_path="recordings/t/c.ogg"
    )
    assert egress_id == "EG_123"
    req = egress.start_requests[0]
    assert req.room_name == "call--t--c"
    assert req.audio_only is True
    assert req.file_outputs[0].gcs.bucket == "vera-recordings"
    assert req.file_outputs[0].filepath == "recordings/t/c.ogg"


async def test_start_failure_raises_domain_error() -> None:
    import aiohttp

    egress = _FakeEgress(raise_on_start=aiohttp.ClientError("boom"))
    gw = _gateway_with(egress)
    with pytest.raises(EgressStartError):
        await gw.start_room_audio_egress("r", bucket="b", object_path="p.ogg")


async def test_get_egress_status_maps_complete() -> None:
    item = SimpleNamespace(
        status=api.EgressStatus.EGRESS_COMPLETE,
        file_results=[SimpleNamespace(duration=90_000_000_000, size=1234)],  # 90s in ns
    )
    gw = _gateway_with(_FakeEgress(list_items=[item]))
    state = await gw.get_egress_status("EG_123")
    assert state is not None
    assert state.complete and not state.failed
    assert state.duration_ms == 90_000
    assert state.size_bytes == 1234


async def test_get_egress_status_unknown_id_returns_none() -> None:
    gw = _gateway_with(_FakeEgress(list_items=[]))
    assert await gw.get_egress_status("EG_GONE") is None
```

- [ ] **Step 2: Run to verify failure** — `just test tests/unit/control_plane/test_livekit_gateway_egress.py -v` → FAIL (`ImportError: EgressStartError`).

- [ ] **Step 3: Implement** — in `livekit_gateway.py`, add below `OutboundDialError`:

```python
class EgressStartError(Exception):
    """Starting the room-composite recording egress failed (LiveKit unreachable or
    the egress service rejected the request). Callers fail OPEN: the call proceeds
    unrecorded and the failure is audited (spec decision 2)."""


class EgressState(NamedTuple):
    """Reconciled egress status for the recording verifier."""

    complete: bool
    failed: bool
    duration_ms: int | None
    size_bytes: int | None
```

(`from typing import NamedTuple` at top.) Add methods to `LiveKitGateway`:

```python
    async def start_room_audio_egress(
        self, room_name: str, *, bucket: str, object_path: str
    ) -> str:
        """Start an audio-only room-composite egress uploading straight to GCS.

        Empty GCSUpload credentials → the egress service signs with its own
        service account (Workload Identity; devops-todo). Returns the egress id
        the verifier later reconciles with ListEgress.
        """
        try:
            async with self._client() as lk:
                info = await lk.egress.start_room_composite_egress(
                    api.RoomCompositeEgressRequest(
                        room_name=room_name,
                        audio_only=True,
                        file_outputs=[
                            api.EncodedFileOutput(
                                file_type=api.EncodedFileType.OGG,
                                filepath=object_path,
                                gcs=api.GCSUpload(bucket=bucket),
                            )
                        ],
                    )
                )
        except _LIVEKIT_TRANSPORT_ERRORS as e:
            raise EgressStartError(str(e)) from e
        return str(info.egress_id)

    _FAILED_EGRESS_STATUSES = frozenset(
        {
            api.EgressStatus.EGRESS_FAILED,
            api.EgressStatus.EGRESS_ABORTED,
            api.EgressStatus.EGRESS_LIMIT_REACHED,
        }
    )

    async def get_egress_status(self, egress_id: str) -> EgressState | None:
        """Current state of one egress; None when LiveKit no longer lists the id
        (server restarted / id expired) — the verifier marks such rows FAILED."""
        async with self._client() as lk:
            resp = await lk.egress.list_egress(api.ListEgressRequest(egress_id=egress_id))
        if not resp.items:
            return None
        item = resp.items[0]
        file_result = item.file_results[0] if item.file_results else None
        return EgressState(
            complete=item.status == api.EgressStatus.EGRESS_COMPLETE,
            failed=item.status in self._FAILED_EGRESS_STATUSES,
            # proto duration is nanoseconds; 0 means "not reported yet".
            duration_ms=(file_result.duration // 1_000_000) if file_result and file_result.duration else None,
            size_bytes=file_result.size if file_result and file_result.size else None,
        )
```

Note for the implementer: if mypy flags `api.EgressStatus` member access or the request kwarg names, check the installed `livekit-api` protocol stubs (`python -c "from livekit import api; print(api.RoomCompositeEgressRequest.DESCRIPTOR.fields_by_name.keys())"`) and adjust the field names to the installed version — do not silence with blanket ignores.

- [ ] **Step 4: Verify** — `just test tests/unit/control_plane/test_livekit_gateway_egress.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(livekit): audio-only room-composite egress start + status wrappers"`

---

### Task 4: RecordingStorage — GCS ops (sha256, delete, exists, signed URL) + in-memory fake

**Files:**
- Create: `apps/control_plane/src/control_plane/recording_storage.py`
- Test: `tests/unit/control_plane/test_recording_storage.py`

**Interfaces:**
- Produces (consumed by Tasks 8, 9, 11):
  - `def parse_gcs_uri(uri: str) -> tuple[str, str]` — `"gs://bucket/a/b.ogg"` → `("bucket", "a/b.ogg")`; raises `ValueError` on non-`gs://`.
  - `class RecordingStorage(Protocol)` with: `async def sha256_and_size(self, bucket: str, object_path: str) -> tuple[str, int] | None` (None = object absent), `async def delete(self, bucket: str, object_path: str) -> None` (absent = no-op), `async def exists(self, bucket: str, object_path: str) -> bool`, `async def signed_url(self, bucket: str, object_path: str, *, ttl_seconds: int) -> str`.
  - `class GCSRecordingStorage` (prod) and `class InMemoryRecordingStorage` (tests: `.objects: dict[tuple[str, str], bytes]`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/control_plane/test_recording_storage.py
"""RecordingStorage contract via the in-memory fake + gs:// parsing."""

import hashlib

import pytest

from control_plane.recording_storage import InMemoryRecordingStorage, parse_gcs_uri


def test_parse_gcs_uri() -> None:
    assert parse_gcs_uri("gs://bkt/a/b/c.ogg") == ("bkt", "a/b/c.ogg")
    with pytest.raises(ValueError):
        parse_gcs_uri("https://bkt/a.ogg")


async def test_sha256_and_size_roundtrip() -> None:
    store = InMemoryRecordingStorage()
    body = b"fake-ogg-bytes"
    store.objects[("bkt", "t/c.ogg")] = body
    result = await store.sha256_and_size("bkt", "t/c.ogg")
    assert result == (hashlib.sha256(body).hexdigest(), len(body))
    assert await store.sha256_and_size("bkt", "missing.ogg") is None


async def test_delete_is_idempotent_and_exists_flips() -> None:
    store = InMemoryRecordingStorage()
    store.objects[("bkt", "x.ogg")] = b"x"
    assert await store.exists("bkt", "x.ogg")
    await store.delete("bkt", "x.ogg")
    await store.delete("bkt", "x.ogg")  # absent → no-op, no raise
    assert not await store.exists("bkt", "x.ogg")


async def test_signed_url_embeds_ttl() -> None:
    store = InMemoryRecordingStorage()
    store.objects[("bkt", "x.ogg")] = b"x"
    url = await store.signed_url("bkt", "x.ogg", ttl_seconds=600)
    assert url.startswith("https://") and "x.ogg" in url and "600" in url
```

- [ ] **Step 2: Run to verify failure** — module not found.

- [ ] **Step 3: Implement**

```python
# apps/control_plane/src/control_plane/recording_storage.py
"""GCS operations for call recordings: sha256 verification, retention deletion,
and V4 signed playback URLs. google-cloud-storage is a sync SDK — every call is
wrapped in asyncio.to_thread (mirrors vera_core/audit/gcs_anchor.py). The signed
URL is minted via IAM signBlob under Workload Identity (no exported key files;
devops-todo grants roles/iam.serviceAccountTokenCreator to the control-plane SA).

Object paths carry only tenant/call UUIDs — never PHI (bright line: no PHI in a
URL or path). The audio bytes themselves are PHI: this module never logs content,
only ids/sizes/hashes.
"""

import asyncio
import hashlib
from datetime import timedelta
from typing import Protocol

_GCS_SCHEME = "gs://"
_CHUNK = 1 << 20  # 1 MiB read chunks for hashing


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Split "gs://bucket/path/to/object" into (bucket, object_path)."""
    if not uri.startswith(_GCS_SCHEME):
        raise ValueError(f"not a gs:// uri: {uri!r}")
    bucket, _, object_path = uri.removeprefix(_GCS_SCHEME).partition("/")
    if not bucket or not object_path:
        raise ValueError(f"malformed gs:// uri: {uri!r}")
    return bucket, object_path


class RecordingStorage(Protocol):
    async def sha256_and_size(self, bucket: str, object_path: str) -> tuple[str, int] | None: ...
    async def delete(self, bucket: str, object_path: str) -> None: ...
    async def exists(self, bucket: str, object_path: str) -> bool: ...
    async def signed_url(self, bucket: str, object_path: str, *, ttl_seconds: int) -> str: ...


class GCSRecordingStorage:
    async def sha256_and_size(self, bucket: str, object_path: str) -> tuple[str, int] | None:
        return await asyncio.to_thread(self._sha256_sync, bucket, object_path)

    def _sha256_sync(self, bucket: str, object_path: str) -> tuple[str, int] | None:
        from google.cloud import storage  # type: ignore[attr-defined]  # lazy prod-only dep

        blob = storage.Client().bucket(bucket).blob(object_path)
        if not blob.exists():
            return None
        digest = hashlib.sha256()
        size = 0
        with blob.open("rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    async def delete(self, bucket: str, object_path: str) -> None:
        await asyncio.to_thread(self._delete_sync, bucket, object_path)

    def _delete_sync(self, bucket: str, object_path: str) -> None:
        from google.api_core.exceptions import NotFound
        from google.cloud import storage  # type: ignore[attr-defined]

        try:
            storage.Client().bucket(bucket).blob(object_path).delete()
        except NotFound:
            return  # already gone — sweep retries / replica races are no-ops

    async def exists(self, bucket: str, object_path: str) -> bool:
        return await asyncio.to_thread(self._exists_sync, bucket, object_path)

    def _exists_sync(self, bucket: str, object_path: str) -> bool:
        from google.cloud import storage  # type: ignore[attr-defined]

        return bool(storage.Client().bucket(bucket).blob(object_path).exists())

    async def signed_url(self, bucket: str, object_path: str, *, ttl_seconds: int) -> str:
        return await asyncio.to_thread(self._signed_url_sync, bucket, object_path, ttl_seconds)

    def _signed_url_sync(self, bucket: str, object_path: str, ttl_seconds: int) -> str:
        import google.auth
        from google.auth.transport import requests as ga_requests
        from google.cloud import storage  # type: ignore[attr-defined]

        # V4 signing without a key file: the ambient SA (Workload Identity) signs
        # via IAM signBlob — requires roles/iam.serviceAccountTokenCreator on itself.
        credentials, _project = google.auth.default()
        credentials.refresh(ga_requests.Request())
        blob = storage.Client(credentials=credentials).bucket(bucket).blob(object_path)
        return str(
            blob.generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=ttl_seconds),
                service_account_email=credentials.service_account_email,
                access_token=credentials.token,
            )
        )


class InMemoryRecordingStorage:
    """Test/dev double: bytes in a dict, deterministic 'signed' URLs."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def sha256_and_size(self, bucket: str, object_path: str) -> tuple[str, int] | None:
        body = self.objects.get((bucket, object_path))
        if body is None:
            return None
        return hashlib.sha256(body).hexdigest(), len(body)

    async def delete(self, bucket: str, object_path: str) -> None:
        self.objects.pop((bucket, object_path), None)

    async def exists(self, bucket: str, object_path: str) -> bool:
        return (bucket, object_path) in self.objects

    async def signed_url(self, bucket: str, object_path: str, *, ttl_seconds: int) -> str:
        return f"https://storage.local/{bucket}/{object_path}?ttl={ttl_seconds}"
```

- [ ] **Step 4: Verify** — `just test tests/unit/control_plane/test_recording_storage.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(recording): GCS storage ops (sha256, delete, signed URL) + fake"`

---

### Task 5: Recording starter — fail-open egress kickoff wired into both call-creation sites

**Files:**
- Create: `packages/vera_core/src/vera_core/services/recordings.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (`start_call` ~line 168, `update_call_status` ~line 534)
- Modify: `packages/vera_core/src/vera_core/services/queue_dispatcher.py` (`try_dispatch`)
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py` (`try_dispatch` call site ~line 887)
- Test: `tests/unit/services/test_recording_starter.py`

**Interfaces:**
- Consumes: Task 3's `start_room_audio_egress` (via duck-typed gateway) + `EgressStartError`; Task 1's `Recording`/`RecordingStatus`/`AuditEvent`.
- Produces (consumed by Task 8's poller via DB rows, and by both call sites):
  - `@dataclass(frozen=True) class RecordingConfig: bucket: str; prefix: str`
  - `def recording_config_from(settings: Settings) -> RecordingConfig | None` — None when `recording_bucket` unset (recording disabled).
  - `def recording_object_path(config: RecordingConfig, tenant_id: UUID, call_id: UUID) -> str`
  - `async def start_recording_for_call(session, livekit, *, config: RecordingConfig, tenant_id: UUID, call_id: UUID, audit: AuditSink | None = None) -> None` — NEVER raises (fail-open).
  - `try_dispatch(..., recording: RecordingConfig | None = None)` — new keyword param.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/services/test_recording_starter.py
"""Fail-open egress kickoff: PENDING row on success, FAILED row + audit on error."""

from uuid import uuid4

from vera_core.audit import AuditRecord
from vera_core.models import Recording
from vera_core.models.enums import RecordingStatus
from vera_core.services.recordings import (
    RecordingConfig,
    recording_object_path,
    start_recording_for_call,
)


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)


class _FakeAudit:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def emit(self, record: AuditRecord) -> None:
        self.records.append(record)


class _OkGateway:
    async def start_room_audio_egress(self, room_name: str, *, bucket: str, object_path: str) -> str:
        return "EG_OK"


class _BoomGateway:
    async def start_room_audio_egress(self, room_name: str, *, bucket: str, object_path: str) -> str:
        raise RuntimeError("egress unreachable")


CONFIG = RecordingConfig(bucket="vera-rec", prefix="recordings")


def test_object_path_is_uuid_only() -> None:
    t, c = uuid4(), uuid4()
    assert recording_object_path(CONFIG, t, c) == f"recordings/{t}/{c}.ogg"


async def test_success_inserts_pending_row() -> None:
    session, t, c = _FakeSession(), uuid4(), uuid4()
    await start_recording_for_call(
        session, _OkGateway(), config=CONFIG, tenant_id=t, call_id=c
    )
    (row,) = session.added
    assert isinstance(row, Recording)
    assert row.status == RecordingStatus.PENDING.value
    assert row.egress_id == "EG_OK"
    assert row.gcs_uri == f"gs://vera-rec/recordings/{t}/{c}.ogg"


async def test_failure_is_fail_open_with_failed_row_and_audit() -> None:
    session, audit, t, c = _FakeSession(), _FakeAudit(), uuid4(), uuid4()
    # Must NOT raise — the call proceeds unrecorded (spec decision 2).
    await start_recording_for_call(
        session, _BoomGateway(), config=CONFIG, tenant_id=t, call_id=c, audit=audit
    )
    (row,) = session.added
    assert isinstance(row, Recording)
    assert row.status == RecordingStatus.FAILED.value
    assert row.egress_id is None
    assert audit.records[0].event_type == "recording.start_failed"
```

- [ ] **Step 2: Run to verify failure** — module not found.

- [ ] **Step 3: Implement `vera_core/services/recordings.py`**

```python
"""Recording kickoff: start the audio-only composite egress and insert the
Recording row, at both call-creation sites (manual /calls and the queue
dispatcher). FAIL-OPEN by design (spec decision 2): a recording that cannot
start must never block a payer call — the failure is recorded as a FAILED row
plus a RECORDING_START_FAILED audit event, and the call proceeds.

No PHI here: room names, buckets, and object paths carry only opaque UUIDs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vera_core.audit import AuditRecord
from vera_core.models import Recording
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import RecordingStatus
from vera_core.observability.correlation import room_name_for_call

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from vera_core.audit import AuditSink
    from vera_core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordingConfig:
    bucket: str
    prefix: str


def recording_config_from(settings: Settings) -> RecordingConfig | None:
    """None ⇒ recording disabled end-to-end (bucket unset — local dev, CI)."""
    if settings.recording_bucket is None:
        return None
    return RecordingConfig(bucket=settings.recording_bucket, prefix=settings.recording_prefix)


def recording_object_path(config: RecordingConfig, tenant_id: UUID, call_id: UUID) -> str:
    """Opaque-UUID object path — no PHI in paths (bright line)."""
    key = f"{tenant_id}/{call_id}.ogg"
    prefix = config.prefix.strip("/")
    return f"{prefix}/{key}" if prefix else key


async def start_recording_for_call(
    session: AsyncSession | Any,
    livekit: Any,  # duck-typed like try_dispatch's gateway param
    *,
    config: RecordingConfig,
    tenant_id: UUID,
    call_id: UUID,
    audit: AuditSink | None = None,
) -> None:
    """Start egress + insert the Recording row. Never raises (fail-open)."""
    room_name = room_name_for_call(tenant_id, call_id)
    object_path = recording_object_path(config, tenant_id, call_id)
    gcs_uri = f"gs://{config.bucket}/{object_path}"
    try:
        egress_id = await livekit.start_room_audio_egress(
            room_name, bucket=config.bucket, object_path=object_path
        )
    except Exception:
        logger.exception("recording: egress start failed for call %s — call proceeds", call_id)
        session.add(
            Recording(
                tenant_id=tenant_id,
                call_id=call_id,
                gcs_uri=gcs_uri,
                status=RecordingStatus.FAILED.value,
            )
        )
        if audit is not None:
            await audit.emit(
                AuditRecord(
                    tenant_id=tenant_id,
                    actor_type=ActorType.SYSTEM,
                    actor_label="recording-starter",
                    event_type=AuditEvent.RECORDING_START_FAILED.value,
                    resource_type="call",
                    resource_id=str(call_id),
                )
            )
        return
    session.add(
        Recording(
            tenant_id=tenant_id,
            call_id=call_id,
            gcs_uri=gcs_uri,
            status=RecordingStatus.PENDING.value,
            egress_id=egress_id,
        )
    )
```

- [ ] **Step 4: Wire the manual call path** — in `calls.py` `start_call`: add deps `audit: Audit` and `settings: AppSettings` (both already exported from `api/v1/common.py`) to the signature, and after the `await livekit.create_call_room(room_name, metadata=metadata)` line insert:

```python
    if (rec_config := recording_config_from(settings)) is not None:
        await start_recording_for_call(
            session, livekit, config=rec_config, tenant_id=tenant_id, call_id=call.id, audit=audit
        )
```

Also, just BEFORE `create_call_room`, force live-transcript publishing for real calls (today it's Voice-Lab-opt-in only; the finalizer needs the stream for every call — spec §4):

```python
    # Real calls always publish the tokenized live transcript — the persistence
    # finalizer (worker_events call.ended handler) drains it into Postgres.
    metadata["publish_transcript"] = True
```

Imports: `from vera_core.services.recordings import recording_config_from, start_recording_for_call` and add `Audit`, `AppSettings` to the `common` import.

- [ ] **Step 5: Wire the dispatcher path** — in `queue_dispatcher.py`:
  - `try_dispatch` signature: add keyword param `recording: RecordingConfig | None = None` (import under `TYPE_CHECKING`: `from vera_core.services.recordings import RecordingConfig`; runtime import of `start_recording_for_call` at top).
  - In the metadata build (after `metadata = tweak.model_dump(exclude_none=True)`): `metadata["publish_transcript"] = True`.
  - After the `async with session.begin_nested():` block succeeds (immediately after `dispatched += 1` is set — i.e. right before the `logger.info("dispatch: initiated call ...")` line), insert:

```python
            if recording is not None:
                # Fail-open and OUTSIDE the savepoint: a recording failure must not
                # undo a successfully dispatched call.
                await start_recording_for_call(
                    session, livekit, config=recording,
                    tenant_id=tenant_id, call_id=call.id, audit=audit,
                )
```

  - Update both `try_dispatch` call sites to pass the config (each router already has `settings: AppSettings` or add it):
    - `calls.py:534` → `await try_dispatch(session, tenant_id, livekit, audit=audit, recording=recording_config_from(settings))` (add `settings: AppSettings` to `update_call_status`'s deps).
    - `patient_forms.py:887` → same change (add the `AppSettings` dep + import if missing).

- [ ] **Step 6: Verify** — `just test tests/unit/services/test_recording_starter.py -v` → PASS, then `just test tests/unit -v` (existing `try_dispatch`/`start_call` tests must still pass — the new param defaults to `None`, metadata gains one key; fix any test asserting exact metadata equality by adding `"publish_transcript": True`).

- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat(recording): fail-open egress kickoff at both call-creation sites"`

---

### Task 6: CallEndedEvent + worker emission + non-blocking transcript drain

**Files:**
- Modify: `packages/vera_core/src/vera_core/events/worker.py`
- Modify: `packages/vera_core/src/vera_core/transcript.py` (`read_all` on both stores + `TranscriptService.drain`; NOT the blocking `read`)
- Modify: `packages/vera_core/src/vera_core/config/settings.py` (`transcript_end_grace_seconds` 60 → 900)
- Modify: `apps/agent_worker/src/agent_worker/main.py` (`_on_shutdown`, ~line 286)
- Test: `tests/unit/events/test_call_ended_event.py` (create), `tests/unit/transcript/test_drain.py` (create)

**Interfaces:**
- Produces (consumed by Task 7):
  - `class CallEndedEvent(BaseModel)`: `type: Literal["call.ended"] = "call.ended"`, `room_name: str`, `ts: int` (epoch ms). `WorkerEvent = CallFailedEvent | CallEndedEvent` (discriminated on `type`); `parse_worker_event` handles both.
  - `TranscriptService.drain(room_name) -> list[TranscriptEvent]` — non-blocking snapshot; missing/expired stream → `[]` (never hangs, unlike `collect()`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/events/test_call_ended_event.py
"""call.ended joins the worker-event union; parse round-trips both types."""

from vera_core.events import CallEndedEvent, CallFailedEvent, parse_worker_event


def test_parse_call_ended_roundtrip() -> None:
    event = CallEndedEvent(room_name="call--t--c", ts=1)
    parsed = parse_worker_event(event.model_dump_json())
    assert isinstance(parsed, CallEndedEvent)
    assert parsed.room_name == "call--t--c"


def test_parse_call_failed_still_works() -> None:
    raw = '{"type":"call.failed","room_name":"r","reason":"no_answer","ts":2}'
    assert isinstance(parse_worker_event(raw), CallFailedEvent)
```

```python
# tests/unit/transcript/test_drain.py
"""drain(): non-blocking snapshot for the finalizer — [] on a missing stream,
sentinel excluded, order preserved."""

from vera_core.transcript import InMemoryTranscriptStore, TranscriptService


async def test_drain_missing_stream_returns_empty() -> None:
    service = TranscriptService(InMemoryTranscriptStore())
    assert await service.drain("no-such-room") == []


async def test_drain_returns_all_turns_excluding_sentinel() -> None:
    service = TranscriptService(InMemoryTranscriptStore())
    await service.publish_turn("room", "user", "[[NAME_1]] calling", ts=1)
    await service.publish_turn("room", "agent", "hello [[NAME_1]]", ts=2)
    await service.end("room")
    events = await service.drain("room")
    assert [(e.role, e.text) for e in events] == [
        ("user", "[[NAME_1]] calling"),
        ("agent", "hello [[NAME_1]]"),
    ]
```

- [ ] **Step 2: Run to verify failure** — both fail with ImportError/AttributeError.

- [ ] **Step 3: Implement `events/worker.py`** — add after `CallFailedEvent`:

```python
class CallEndedEvent(BaseModel):
    """Emitted by the worker at session shutdown for a canonical call room, AFTER
    the transcript ended-sentinel is written — the control plane's trigger to
    persist the tokenized transcript to Postgres. PHI-free: room name + ts only."""

    type: Literal["call.ended"] = "call.ended"
    room_name: str
    ts: int  # epoch milliseconds
```

Replace the union/adapter lines:

```python
type WorkerEvent = CallFailedEvent | CallEndedEvent
_ADAPTER: TypeAdapter[WorkerEvent] = TypeAdapter(
    Annotated[CallFailedEvent | CallEndedEvent, Field(discriminator="type")]
)
```

(imports: `from typing import Annotated, Literal`, `from pydantic import BaseModel, Field, TypeAdapter`). Export `CallEndedEvent` from `vera_core/events/__init__.py` alongside the existing names.

- [ ] **Step 4: Implement `transcript.py` drain** — add to the `TranscriptStore` Protocol:

```python
    async def read_all(self, room_name: str) -> list[TranscriptEvent]: ...
```

`InMemoryTranscriptStore`:

```python
    async def read_all(self, room_name: str) -> list[TranscriptEvent]:
        entries = self._entries.get(transcript_stream_key(room_name), [])
        return [
            TranscriptEvent(role=f["role"], text=f["text"], ts=int(f["ts"]))
            for _id, f in entries
            if f.get(_ENDED_FIELD) != _ENDED_VALUE
        ]
```

`RedisTranscriptStore`:

```python
    async def read_all(self, room_name: str) -> list[TranscriptEvent]:
        # XRANGE full-range: non-blocking, no sentinel dependency — an expired or
        # never-created stream returns []. This is the finalizer's read; the SSE
        # path keeps the tailing read().
        entries = await self._redis.xrange(transcript_stream_key(room_name))
        return [
            TranscriptEvent(role=fields["role"], text=fields["text"], ts=int(fields["ts"]))
            for _entry_id, fields in entries
            if fields.get(_ENDED_FIELD) != _ENDED_VALUE
        ]
```

`TranscriptService`:

```python
    async def drain(self, room_name: str) -> list[TranscriptEvent]:
        """Non-blocking snapshot of everything currently in the stream (the
        persistence finalizer). Unlike collect(), never blocks: a missing or
        already-expired stream yields []."""
        return await self._store.read_all(room_name)
```

- [ ] **Step 5: Bump the end-grace default** — in `settings.py` change `transcript_end_grace_seconds: int = 60` to `900`, and extend the comment above the pair:

```python
    # Live-transcript Redis stream lifetime (Voice Lab / SSE). The rolling backstop
    # TTL is refreshed on every publish so an abandoned stream self-clears; the end
    # grace TTL lets connected readers drain the `ended` sentinel before it clears.
    # The grace window is also the persistence-finalizer's durability budget: the
    # control plane must consume call.ended and drain the stream before it expires,
    # so it is sized to ride out a control-plane restart, not just an SSE drain.
```

- [ ] **Step 6: Worker emission** — in `agent_worker/main.py`, extend the import to `from vera_core.events import CallEndedEvent, CallFailedEvent, CallFailureReason, WorkerEventBus`, and in `_on_shutdown` insert AFTER the `transcript_service.end(...)` block and BEFORE the `transcript_redis.aclose()` block (order is load-bearing: sentinel first, then the event that triggers draining):

```python
        if transcript_service is not None and parse_room_name(room_name) is not None:
            events_redis = create_redis(settings.redis_url)
            try:
                bus = WorkerEventBus(events_redis, maxlen=settings.worker_events_stream_maxlen)
                await bus.emit(CallEndedEvent(room_name=room_name, ts=int(time.time() * 1000)))
            except Exception:  # best-effort; never block shutdown
                logger.exception("failed to emit call.ended for %s", room_name)
            finally:
                await events_redis.aclose()
```

(`parse_room_name` and `time` are already imported in that module; verify.)

- [ ] **Step 7: Verify** — `just test tests/unit/events tests/unit/transcript -v` → PASS (including pre-existing tests: the widened adapter must not break `call.failed` parsing).

- [ ] **Step 8: Commit** — `git add -A && git commit -m "feat(events): call.ended worker event + non-blocking transcript drain"`

---

### Task 7: Transcript finalizer — call.ended handler in WorkerEventConsumer

**Files:**
- Modify: `apps/control_plane/src/control_plane/worker_events.py`
- Modify: `apps/control_plane/src/control_plane/main.py` (consumer wiring, ~line 136)
- Test: `tests/unit/control_plane/test_transcript_finalizer.py` (create)

**Interfaces:**
- Consumes: Task 6's `CallEndedEvent` + `TranscriptService.drain`; existing `tenant_session`, `Transcript` model, `TranscriptSource`, `ROLE_USER`.
- Produces: `WorkerEventConsumer.__init__` gains keyword params `sessionmaker: async_sessionmaker[AsyncSession] | None = None`, `transcripts: TranscriptService | None = None` — the `call.ended` handler registers only when both are provided (existing tests/wiring unchanged). Inserted `transcript` rows: `seq` = stream order (0-based), `source` = `rep` for role `user` / `bot` for role `agent`, `message` = tokenized text as-is, `spoke_at` = event ts.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/control_plane/test_transcript_finalizer.py
"""call.ended → drain the stream → idempotent transcript insert (unit level:
assert the row payloads the handler builds; DB idempotency is the ON CONFLICT
clause, exercised in the integration suite)."""

from uuid import uuid4

from control_plane.worker_events import WorkerEventConsumer, build_transcript_rows
from vera_core.events import CallEndedEvent
from vera_core.models.enums import TranscriptSource
from vera_core.observability.correlation import room_name_for_call
from vera_core.transcript import InMemoryTranscriptStore, TranscriptService


def test_build_transcript_rows_maps_roles_and_seq() -> None:
    tenant_id, call_id = uuid4(), uuid4()
    from vera_core.transcript import TranscriptEvent

    events = [
        TranscriptEvent(role="user", text="[[NAME_1]] speaking", ts=1_700_000_000_000),
        TranscriptEvent(role="agent", text="hello [[NAME_1]]", ts=1_700_000_001_000),
    ]
    rows = build_transcript_rows(tenant_id, call_id, events)
    assert [r["seq"] for r in rows] == [0, 1]
    assert rows[0]["source"] == TranscriptSource.REP.value
    assert rows[1]["source"] == TranscriptSource.BOT.value
    assert rows[0]["message"] == "[[NAME_1]] speaking"
    assert rows[0]["tenant_id"] == tenant_id and rows[0]["call_id"] == call_id
    assert rows[0]["spoke_at"].timestamp() == 1_700_000_000.0
    assert all("id" in r for r in rows)  # bulk insert bypasses the ORM client default


async def test_handler_registered_only_with_deps() -> None:
    class _R:  # never touched in this test
        pass

    bare = WorkerEventConsumer(_R(), livekit=None)  # type: ignore[arg-type]
    assert "call.ended" not in bare._handlers
    wired = WorkerEventConsumer(
        _R(),  # type: ignore[arg-type]
        livekit=None,  # type: ignore[arg-type]
        sessionmaker=object(),  # type: ignore[arg-type]
        transcripts=TranscriptService(InMemoryTranscriptStore()),
    )
    assert "call.ended" in wired._handlers
```

Note: if `WorkerEventConsumer.__init__` requires a real gateway, adjust `livekit` typing to `LiveKitGateway | None` as part of Step 3 (the `call.failed` handler already guards; a `None` gateway simply means that handler would fail — acceptable, it is never registered without one in production wiring. If mypy objects, keep the param required and pass a `SimpleNamespace()` in the test instead).

- [ ] **Step 2: Run to verify failure** — ImportError on `build_transcript_rows`.

- [ ] **Step 3: Implement** — in `worker_events.py`:

Add imports:

```python
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.db import tenant_session
from vera_core.db.base import uuid7  # same default the PK mixin uses
from vera_core.events import CallEndedEvent  # extend the existing import block
from vera_core.models import Transcript
from vera_core.models.enums import TranscriptSource
from vera_core.transcript import ROLE_USER, TranscriptEvent, TranscriptService
```

(Verify `uuid7`'s import path with `grep -n "uuid7" packages/vera_core/src/vera_core/db/base.py` — import it from wherever `base.py` gets it.)

Module-level helper (unit-testable without a DB):

```python
def build_transcript_rows(
    tenant_id: UUID, call_id: UUID, events: list[TranscriptEvent]
) -> list[dict[str, object]]:
    """Bulk-insert payloads for the transcript table. seq is the stream order
    (the producer already guarantees chronological order — see
    TranscriptService.consume); text is tokenized and persisted AS-IS (spec
    decision 3 — never hydrate). Explicit ids: executemany INSERTs bypass the
    ORM client-side default."""
    return [
        {
            "id": uuid7(),
            "tenant_id": tenant_id,
            "call_id": call_id,
            "seq": seq,
            "source": (
                TranscriptSource.REP.value if event.role == ROLE_USER else TranscriptSource.BOT.value
            ),
            "role": event.role,
            "message": event.text,
            "spoke_at": datetime.fromtimestamp(event.ts / 1000, tz=UTC),
        }
        for seq, event in enumerate(events)
    ]
```

Constructor: add keyword params and conditional registration:

```python
    def __init__(
        self,
        redis: Redis,
        livekit: LiveKitGateway,
        *,
        block_ms: int = 5_000,
        reclaim_idle_ms: int = 60_000,
        teardown_grace_ms: int = 1_500,
        consumer_name: str | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        transcripts: TranscriptService | None = None,
    ) -> None:
        ...existing body...
        self._sessionmaker = sessionmaker
        self._transcripts = transcripts
        self._handlers: dict[str, EventHandler] = {"call.failed": self._handle_call_failed}
        # Persistence finalizer is opt-in: only when the control plane hands us a
        # DB sessionmaker + transcript service (tests / minimal wiring skip it).
        if sessionmaker is not None and transcripts is not None:
            self._handlers["call.ended"] = self._handle_call_ended
```

Handler:

```python
    async def _handle_call_ended(self, event: WorkerEvent) -> None:
        if not isinstance(event, CallEndedEvent):
            return
        if self._sessionmaker is None or self._transcripts is None:  # pragma: no cover
            return
        ref = parse_room_name(event.room_name)
        if ref is None:
            logger.warning("call.ended for non-vera room %s; ignoring", event.room_name)
            return
        events = await self._transcripts.drain(event.room_name)
        if not events:
            # Expired stream (grace TTL elapsed before we ran) or a call with no
            # finalized turns. Nothing to persist; rows from an earlier delivery,
            # if any, are already in place.
            logger.warning("call.ended: no transcript entries for %s", event.room_name)
            return
        rows = build_transcript_rows(ref.tenant_id, ref.call_id, events)
        async with tenant_session(self._sessionmaker, ref.tenant_id) as session:
            # Idempotent under at-least-once delivery: UNIQUE(call_id, seq) +
            # ON CONFLICT DO NOTHING makes a redelivered event a no-op.
            await session.execute(
                pg_insert(Transcript).values(rows).on_conflict_do_nothing(
                    index_elements=["call_id", "seq"]
                )
            )
        logger.info(
            "persisted %d transcript rows for call %s", len(rows), ref.call_id
        )
```

- [ ] **Step 4: Wire in `main.py`** — the consumer block (~line 136) gains the two kwargs:

```python
            consumer = WorkerEventConsumer(
                worker_events_redis,
                app.state.livekit,
                block_ms=settings.worker_events_block_ms,
                reclaim_idle_ms=settings.worker_events_reclaim_idle_ms,
                teardown_grace_ms=settings.call_failed_teardown_grace_ms,
                sessionmaker=sessionmaker,
                transcripts=_transcript_service,
            )
```

- [ ] **Step 5: Integration test (DB idempotency)** — add to `tests/integration/transcript/` (skips without `just up`, following that directory's conftest conventions):

```python
# tests/integration/transcript/test_finalizer_persistence.py
"""Drain → insert → redeliver → no duplicates (ON CONFLICT path, real Postgres)."""
```

Test body: create tenant + call rows via existing integration fixtures (copy the setup style used in `tests/integration/control_plane/` — whichever fixture creates a tenant + call), publish 2 turns + `end()` on an `InMemoryTranscriptStore`-backed service keyed by the call's room name, construct `WorkerEventConsumer` with the real sessionmaker + that service, call `await consumer._handle_call_ended(CallEndedEvent(room_name=room, ts=1))` **twice**, then assert `SELECT count(*) FROM transcript WHERE call_id = ...` == 2 inside a `tenant_session`.

- [ ] **Step 6: Verify** — `just test tests/unit/control_plane/test_transcript_finalizer.py -v` → PASS; with `just up && just migrate`: `just test tests/integration/transcript -v` → PASS.

- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat(transcript): persist tokenized transcripts on call.ended (idempotent)"`

---

### Task 8: Recording verifier — definer work-list fns + reconciliation poller + lifespan wiring

**Files:**
- Create: migration via `just makemigration "recording work-list definer fns"`
- Create: `apps/control_plane/src/control_plane/recording_jobs.py` (verifier; Task 9 adds the sweeper here)
- Modify: `apps/control_plane/src/control_plane/main.py` (lifespan)
- Test: `tests/unit/control_plane/test_recording_verifier.py`

**Interfaces:**
- Consumes: Task 3 `get_egress_status`/`EgressState`, Task 4 `RecordingStorage`/`parse_gcs_uri`, Task 1 columns/settings.
- Produces:
  - SQL fns (EXECUTE PUBLIC, owner `vera_definer_owner`): `recording_pending_work() RETURNS TABLE(tenant_id uuid, recording_id uuid, call_id uuid, egress_id text, gcs_uri text)`; `recording_retention_due() RETURNS TABLE(tenant_id uuid, recording_id uuid)` (Task 9 consumes the latter).
  - `class RecordingVerifier` with `async def run(self) -> None` (loop) and `async def tick(self) -> None` (one pass, unit-testable). Constructor: `(sessionmaker, livekit, storage, audit, *, interval_seconds: int, retention_days_default: int)`.

- [ ] **Step 1: Migration** — `just makemigration "recording work-list definer fns"`; body (pattern: `0016_audit_chain_heads.py`):

```python
DEFINER_ROLE = "vera_definer_owner"
_SEARCH_PATH = "SET search_path = pg_catalog, public"

_PENDING_FN = f"""
CREATE OR REPLACE FUNCTION recording_pending_work()
RETURNS TABLE(tenant_id uuid, recording_id uuid, call_id uuid, egress_id text, gcs_uri text)
LANGUAGE sql
STABLE
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
    SELECT r.tenant_id, r.id, r.call_id, r.egress_id, r.gcs_uri
      FROM recording r
     WHERE r.status = 'pending';
$$
"""

_RETENTION_FN = f"""
CREATE OR REPLACE FUNCTION recording_retention_due()
RETURNS TABLE(tenant_id uuid, recording_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
    SELECT r.tenant_id, r.id
      FROM recording r
     WHERE r.status = 'available'
       AND r.retention_until IS NOT NULL
       AND r.retention_until < now();
$$
"""


def upgrade() -> None:
    op.execute(_PENDING_FN)
    op.execute(_RETENTION_FN)
    op.execute(f"ALTER FUNCTION recording_pending_work() OWNER TO {DEFINER_ROLE}")
    op.execute(f"ALTER FUNCTION recording_retention_due() OWNER TO {DEFINER_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS recording_pending_work()")
    op.execute("DROP FUNCTION IF EXISTS recording_retention_due()")
```

These are cross-tenant WORK LISTS only (ids + non-PHI pointers); all row mutation happens inside `tenant_session(tenant_id)` with full RLS — the `audit_chain_heads()` precedent.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/control_plane/test_recording_verifier.py
"""Verifier state machine: pending → available (sha256 stamped) / failed /
discarded (no-answer). Uses fakes for gateway+storage and a stub work-list."""

import hashlib
from typing import Any
from uuid import uuid4

import pytest

from control_plane.livekit_gateway import EgressState
from control_plane.recording_jobs import PendingRow, RecordingVerifier
from control_plane.recording_storage import InMemoryRecordingStorage
from vera_core.models.enums import RecordingStatus


class _FakeGateway:
    def __init__(self, state: EgressState | None) -> None:
        self._state = state

    async def get_egress_status(self, egress_id: str) -> EgressState | None:
        return self._state


class _FakeAudit:
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def emit(self, record: Any) -> None:
        self.records.append(record)


@pytest.fixture
def row() -> PendingRow:
    return PendingRow(
        tenant_id=uuid4(),
        recording_id=uuid4(),
        call_id=uuid4(),
        egress_id="EG_1",
        gcs_uri="gs://bkt/recordings/t/c.ogg",
    )


def _verifier(
    gateway: _FakeGateway,
    storage: InMemoryRecordingStorage,
    audit: _FakeAudit,
    monkeypatch: pytest.MonkeyPatch,
    *,
    call_status: str = "completed",
    updates: list[dict[str, Any]],
) -> RecordingVerifier:
    """Build a verifier with the DB seams stubbed out: _apply_update captures the
    payload; the call/tenant lookups are monkeypatched to canned values."""
    verifier = RecordingVerifier(
        sessionmaker=object(),  # type: ignore[arg-type]  # DB seams stubbed below
        livekit=gateway,  # type: ignore[arg-type]
        storage=storage,
        audit=audit,
        interval_seconds=30,
        retention_days_default=90,
    )

    async def _capture(row: PendingRow, *, expected: str, values: dict[str, Any]) -> None:
        updates.append({"expected": expected, **values})

    monkeypatch.setattr(verifier, "_apply_update", _capture)
    # _load_call / _load_retention_days are the verifier's deliberate DB seams
    # (see Step 3) — stub them so no sessionmaker is needed.
    from types import SimpleNamespace

    async def _load_call(row: PendingRow) -> Any:
        return SimpleNamespace(current_status=call_status, ended_at=None)

    async def _load_retention_days(row: PendingRow) -> int:
        return 90

    monkeypatch.setattr(verifier, "_load_call", _load_call)
    monkeypatch.setattr(verifier, "_load_retention_days", _load_retention_days)
    return verifier


async def test_in_progress_egress_applies_nothing(
    row: PendingRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates: list[dict[str, Any]] = []
    verifier = _verifier(
        _FakeGateway(EgressState(complete=False, failed=False, duration_ms=None, size_bytes=None)),
        InMemoryRecordingStorage(),
        _FakeAudit(),
        monkeypatch,
        updates=updates,
    )
    await verifier._verify_one(row)
    assert updates == []


async def test_complete_egress_verifies_sha256_and_stamps_retention(
    row: PendingRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates: list[dict[str, Any]] = []
    storage = InMemoryRecordingStorage()
    body = b"ogg-bytes"
    storage.objects[("bkt", "recordings/t/c.ogg")] = body
    verifier = _verifier(
        _FakeGateway(EgressState(complete=True, failed=False, duration_ms=90_000, size_bytes=9)),
        storage,
        _FakeAudit(),
        monkeypatch,
        updates=updates,
    )
    await verifier._verify_one(row)
    (update,) = updates
    assert update["expected"] == RecordingStatus.PENDING.value
    assert update["status"] == RecordingStatus.AVAILABLE.value
    assert update["sha256"] == hashlib.sha256(body).hexdigest()
    assert update["size_bytes"] == len(body)
    assert update["duration_ms"] == 90_000
    assert update["retention_until"] is not None


async def test_no_answer_call_discards_object(
    row: PendingRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates: list[dict[str, Any]] = []
    storage = InMemoryRecordingStorage()
    storage.objects[("bkt", "recordings/t/c.ogg")] = b"x"
    verifier = _verifier(
        _FakeGateway(EgressState(complete=True, failed=False, duration_ms=1, size_bytes=1)),
        storage,
        _FakeAudit(),
        monkeypatch,
        call_status="no_answer",
        updates=updates,
    )
    await verifier._verify_one(row)
    assert not await storage.exists("bkt", "recordings/t/c.ogg")
    assert updates[0]["status"] == RecordingStatus.DISCARDED.value


async def test_lost_or_failed_egress_marks_failed_and_audits(
    row: PendingRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates: list[dict[str, Any]] = []
    audit = _FakeAudit()
    verifier = _verifier(
        _FakeGateway(None), InMemoryRecordingStorage(), audit, monkeypatch, updates=updates
    )
    await verifier._verify_one(row)
    assert updates[0]["status"] == RecordingStatus.FAILED.value
    assert audit.records[0].event_type == "recording.failed"
```

- [ ] **Step 3: Implement `recording_jobs.py`** (verifier half)

```python
"""Recording background jobs: the egress-reconciliation verifier (this task) and
the retention sweeper (Task 9). Both are control-plane lifespan tasks following
the WorkerEventConsumer loop discipline (never die: log + sleep on error).

Cross-tenant discovery goes through SECURITY DEFINER work-list functions
(recording_pending_work / recording_retention_due — ids and non-PHI pointers
only); every row mutation runs inside tenant_session(...) with full RLS.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.livekit_gateway import LiveKitGateway
from control_plane.recording_storage import RecordingStorage, parse_gcs_uri
from vera_core.audit import AuditRecord, AuditSink
from vera_core.db import tenant_session
from vera_core.models import Call, Recording, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import CallStatus, RecordingStatus

logger = logging.getLogger("control_plane.recording_jobs")

_DISCARD_CALL_STATUSES = frozenset({CallStatus.NO_ANSWER.value, CallStatus.BUSY.value})


@dataclass(frozen=True)
class PendingRow:
    tenant_id: UUID
    recording_id: UUID
    call_id: UUID
    egress_id: str | None
    gcs_uri: str


class RecordingVerifier:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        livekit: LiveKitGateway,
        storage: RecordingStorage,
        audit: AuditSink,
        *,
        interval_seconds: int,
        retention_days_default: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._livekit = livekit
        self._storage = storage
        self._audit = audit
        self._interval = interval_seconds
        self._retention_days_default = retention_days_default

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("recording verifier tick failed; continuing")
            await asyncio.sleep(self._interval)

    async def tick(self) -> None:
        rows = await self._pending_rows()
        for row in rows:
            try:
                await self._verify_one(row)
            except Exception:
                # One bad row must not starve the rest; state-guarded updates make
                # a retry next tick safe.
                logger.exception("verify failed for recording %s", row.recording_id)

    async def _pending_rows(self) -> list[PendingRow]:
        async with self._sessionmaker() as session:
            result = await session.execute(
                text(
                    "SELECT tenant_id, recording_id, call_id, egress_id, gcs_uri"
                    " FROM recording_pending_work()"
                )
            )
            return [PendingRow(*row) for row in result.all()]

    async def _verify_one(self, row: PendingRow) -> None:
        if row.egress_id is None:  # FAILED-at-start rows never enter pending; guard anyway
            return
        state = await self._livekit.get_egress_status(row.egress_id)
        if state is None or state.failed:
            await self._mark_failed(row, reason="egress_lost" if state is None else "egress_failed")
            return
        if not state.complete:
            return  # still recording — next tick

        bucket, object_path = parse_gcs_uri(row.gcs_uri)
        call = await self._load_call(row)

        if call is not None and call.current_status in _DISCARD_CALL_STATUSES:
            # No-answer/busy: nothing worth keeping — delete the object now.
            await self._storage.delete(bucket, object_path)
            await self._apply_update(
                row,
                expected=RecordingStatus.PENDING.value,
                values={
                    "status": RecordingStatus.DISCARDED.value,
                    "deleted_at": func.now(),
                },
            )
            await self._emit(row, AuditEvent.RECORDING_DISCARDED, {"call_status": call.current_status})
            return

        digest = await self._storage.sha256_and_size(bucket, object_path)
        if digest is None:
            return  # upload not visible in GCS yet — retry next tick
        sha256, size_bytes = digest

        days = await self._load_retention_days(row)
        ended_at = call.ended_at if call is not None else None
        retention_until = (
            ended_at + timedelta(days=days)
            if ended_at is not None
            else func.now() + func.make_interval(0, 0, 0, days)  # DB clock, not app clock
        )
        await self._apply_update(
            row,
            expected=RecordingStatus.PENDING.value,
            values={
                "status": RecordingStatus.AVAILABLE.value,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "duration_ms": state.duration_ms,
                "retention_until": retention_until,
            },
        )
        logger.info("recording %s verified (sha256=%s…)", row.recording_id, sha256[:12])

    async def _load_call(self, row: PendingRow) -> Call | None:
        """Small seam so unit tests can stub the DB lookup."""
        async with tenant_session(self._sessionmaker, row.tenant_id) as session:
            return (
                await session.execute(select(Call).where(Call.id == row.call_id))
            ).scalar_one_or_none()

    async def _load_retention_days(self, row: PendingRow) -> int:
        """Tenant override or the platform default (small seam, see _load_call)."""
        async with tenant_session(self._sessionmaker, row.tenant_id) as session:
            tenant = (
                await session.execute(select(Tenant).where(Tenant.id == row.tenant_id))
            ).scalar_one_or_none()
        if tenant is not None and tenant.recording_retention_days is not None:
            return tenant.recording_retention_days
        return self._retention_days_default

    async def _mark_failed(self, row: PendingRow, *, reason: str) -> None:
        await self._apply_update(
            row,
            expected=RecordingStatus.PENDING.value,
            values={"status": RecordingStatus.FAILED.value},
        )
        await self._emit(row, AuditEvent.RECORDING_FAILED, {"reason": reason})

    async def _apply_update(
        self, row: PendingRow, *, expected: str, values: dict[str, Any]
    ) -> None:
        # State-guarded: a replica that already transitioned the row wins; ours no-ops.
        async with tenant_session(self._sessionmaker, row.tenant_id) as session:
            await session.execute(
                update(Recording)
                .where(Recording.id == row.recording_id, Recording.status == expected)
                .values(**values)
            )

    async def _emit(
        self, row: PendingRow, event: AuditEvent, detail: dict[str, Any]
    ) -> None:
        await self._audit.emit(
            AuditRecord(
                tenant_id=row.tenant_id,
                actor_type=ActorType.SYSTEM,
                actor_label="recording-verifier",
                event_type=event.value,
                resource_type="recording",
                resource_id=str(row.recording_id),
                detail={"call_id": str(row.call_id), **detail},
            )
        )
```

- [ ] **Step 4: Lifespan wiring** — in `main.py` lifespan, after the worker-event consumer block:

```python
        # Recording verifier: reconciles PENDING egresses → AVAILABLE (sha256) /
        # FAILED / DISCARDED. Only runs when recording is configured AND LiveKit
        # is available (it queries egress status).
        recording_storage: RecordingStorage | None = None
        verifier_task: asyncio.Task[None] | None = None
        if settings.recording_bucket is not None:
            recording_storage = GCSRecordingStorage()
        app.state.recording_storage = recording_storage
        if recording_storage is not None and app.state.livekit is not None:
            verifier = RecordingVerifier(
                sessionmaker,
                app.state.livekit,
                recording_storage,
                app.state.audit,
                interval_seconds=settings.recording_verify_interval_seconds,
                retention_days_default=settings.recording_retention_days_default,
            )
            verifier_task = asyncio.create_task(verifier.run())
            verifier_task.add_done_callback(_log_background_exit)
```

Rename `_log_consumer_exit` → `_log_background_exit` (generic message `"background task exited unexpectedly"`) and reuse it for all three tasks. Shutdown block: cancel+await `verifier_task` the same way `worker_event_task` is handled. Imports: `from control_plane.recording_jobs import RecordingVerifier`, `from control_plane.recording_storage import GCSRecordingStorage, RecordingStorage`.

- [ ] **Step 5: Verify** — `just test tests/unit/control_plane/test_recording_verifier.py -v` → PASS; `just migrate` applies the fn migration.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(recording): sha256 verification poller with definer work lists"`

---

### Task 9: Retention sweeper — before/after audited deletion

**Files:**
- Modify: `apps/control_plane/src/control_plane/recording_jobs.py` (add `RetentionSweeper`)
- Modify: `apps/control_plane/src/control_plane/main.py` (lifespan)
- Test: `tests/unit/control_plane/test_retention_sweeper.py`

**Interfaces:**
- Consumes: Task 8's `recording_retention_due()` fn + module scaffolding; Task 4 storage.
- Produces: `class RetentionSweeper` — constructor `(sessionmaker, storage, audit, *, interval_seconds: int)`, methods `run()` / `tick()`. Audit contract (spec §5): `RECORDING_DELETED` twice per recording — `detail.phase="before"` with `{gcs_uri, size_bytes, sha256, retention_until}` then `detail.phase="after"` with `{verified_gone: true}`; row tombstoned `status="deleted"`, `deleted_at=func.now()`, sha256/size retained.

- [ ] **Step 1: Write the failing test** — `tests/unit/control_plane/test_retention_sweeper.py` with fakes in the Task 8 style:

1. Due row → before-audit emitted (phase `"before"`, carries `gcs_uri`/`sha256`/`size_bytes`/`retention_until` as strings/ints) → object deleted from `InMemoryRecordingStorage` → after-audit (phase `"after"`, `verified_gone: True`) → update applied with `status="deleted"` guarded on `expected="available"`.
2. `exists()` still true after delete (simulate by a storage fake whose `delete` is a no-op) → NO after-audit, NO tombstone update (retried next tick), error logged.
3. Row no longer `available` when re-checked inside the tenant session (already swept by a replica) → nothing emitted, no storage call.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement `RetentionSweeper`** in `recording_jobs.py`:

```python
class RetentionSweeper:
    """Deletes recordings past retention_until with before/after audit snapshots
    (spec decision 5). GCS delete is idempotent (absent → no-op) and the tombstone
    update is state-guarded, so replicas and retries are safe."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: RecordingStorage,
        audit: AuditSink,
        *,
        interval_seconds: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._storage = storage
        self._audit = audit
        self._interval = interval_seconds

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention sweep tick failed; continuing")
            await asyncio.sleep(self._interval)

    async def tick(self) -> None:
        async with self._sessionmaker() as session:
            result = await session.execute(
                text("SELECT tenant_id, recording_id FROM recording_retention_due()")
            )
            due = [(UUID(str(t)), UUID(str(r))) for t, r in result.all()]
        for tenant_id, recording_id in due:
            try:
                await self._sweep_one(tenant_id, recording_id)
            except Exception:
                logger.exception("sweep failed for recording %s", recording_id)

    async def _sweep_one(self, tenant_id: UUID, recording_id: UUID) -> None:
        async with tenant_session(self._sessionmaker, tenant_id) as session:
            rec = (
                await session.execute(
                    select(Recording).where(
                        Recording.id == recording_id,
                        Recording.status == RecordingStatus.AVAILABLE.value,
                    )
                )
            ).scalar_one_or_none()
        if rec is None:
            return  # already swept (replica) or state changed — nothing to do

        # BEFORE snapshot: what is about to be destroyed (evidence survives in
        # the append-only audit_log even if we crash mid-delete).
        await self._emit_deleted(
            tenant_id,
            recording_id,
            call_id=rec.call_id,
            detail={
                "phase": "before",
                "gcs_uri": rec.gcs_uri,
                "size_bytes": rec.size_bytes,
                "sha256": rec.sha256,
                "retention_until": rec.retention_until.isoformat() if rec.retention_until else None,
            },
        )
        bucket, object_path = parse_gcs_uri(rec.gcs_uri)
        await self._storage.delete(bucket, object_path)
        if await self._storage.exists(bucket, object_path):
            logger.error(
                "recording %s object still present after delete; will retry", recording_id
            )
            return  # no AFTER record, no tombstone — retried next tick

        async with tenant_session(self._sessionmaker, tenant_id) as session:
            await session.execute(
                update(Recording)
                .where(
                    Recording.id == recording_id,
                    Recording.status == RecordingStatus.AVAILABLE.value,
                )
                .values(status=RecordingStatus.DELETED.value, deleted_at=func.now())
            )
        await self._emit_deleted(
            tenant_id,
            recording_id,
            call_id=rec.call_id,
            detail={"phase": "after", "verified_gone": True},
        )

    async def _emit_deleted(
        self, tenant_id: UUID, recording_id: UUID, *, call_id: UUID, detail: dict[str, Any]
    ) -> None:
        await self._audit.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.SYSTEM,
                actor_label="retention-sweeper",
                event_type=AuditEvent.RECORDING_DELETED.value,
                resource_type="recording",
                resource_id=str(recording_id),
                detail={"call_id": str(call_id), **detail},
            )
        )
```

- [ ] **Step 4: Lifespan wiring** — alongside the verifier (sweeper needs storage but NOT LiveKit):

```python
        sweeper_task: asyncio.Task[None] | None = None
        if recording_storage is not None:
            sweeper = RetentionSweeper(
                sessionmaker,
                recording_storage,
                app.state.audit,
                interval_seconds=settings.retention_sweep_interval_seconds,
            )
            sweeper_task = asyncio.create_task(sweeper.run())
            sweeper_task.add_done_callback(_log_background_exit)
```

plus cancel+await on shutdown.

- [ ] **Step 5: Verify** — `just test tests/unit/control_plane/test_retention_sweeper.py -v` → PASS.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(recording): retention sweeper with before/after audit snapshots"`

---

### Task 10: Retention-policy endpoint (GET + PATCH /tenant/config/retention)

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/tenant_config.py`
- Modify: `packages/vera_core/src/vera_core/models/enums.py` (`AuthEvent.RETENTION_POLICY_UPDATED`)
- Modify: `packages/vera_core/src/vera_core/schemas/dto.py` + `schemas/__init__.py` (`RetentionPolicy` DTO)
- Create: migration via `just makemigration "widen auth audit event check for retention policy"`
- Test: `tests/unit/http/test_retention_policy.py` (follow the endpoint-test conventions in `tests/unit/http/`)

**Interfaces:**
- Consumes: Task 1 `Tenant.recording_retention_days` + `recording_retention_days_default` setting; Task 2 `recordings:manage`.
- Produces: `GET/PATCH /api/v1/tenant/config/retention` returning `RetentionPolicy(retention_days: int | None, default_days: int)`; `AuthEvent.RETENTION_POLICY_UPDATED = "retention_policy_updated"`.

- [ ] **Step 1: Write the failing test** — cases: (1) GET returns `retention_days=None` + `default_days=90` for a fresh tenant; (2) PATCH `{"retention_days": 30}` → 200, persisted, auth-audit records `retention_policy_updated` with `meta == {"old_days": None, "new_days": 30}`; (3) PATCH `{"retention_days": 0}` → 422; (4) caller without `recordings:manage` → 403. Copy the app/client/permission-stub fixtures from an existing `tests/unit/http/` router test (e.g. the tenant-config persona tests) verbatim.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

`enums.py` — append to `AuthEvent`:

```python
    RETENTION_POLICY_UPDATED = "retention_policy_updated"
```

`schemas/dto.py` — add (match the module's existing BaseModel style):

```python
class RetentionPolicy(BaseModel):
    """Tenant recording-retention knob. retention_days=None → the platform
    default applies (surfaced as default_days so the UI can render the
    effective value). Bounded: 1 day to 10 years."""

    retention_days: int | None = Field(default=None, ge=1, le=3650)
    default_days: int
```

For PATCH input use a request model without `default_days`:

```python
class RetentionPolicyUpdate(BaseModel):
    retention_days: int | None = Field(default=None, ge=1, le=3650)
```

Export both from `schemas/__init__.py`.

`tenant_config.py` — add (mirrors the persona pair exactly; `AppSettings` from `common`):

```python
@router.get(
    "/tenant/config/retention",
    response_model=ResponseModel[RetentionPolicy],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def get_retention_policy(
    tenant_id: TenantId,
    session: TenantSession,
    settings: AppSettings,
    _caller: VerifiedIdentity = require("recordings:manage"),
) -> ResponseModel[RetentionPolicy]:
    tenant = await _load_tenant(session, tenant_id)
    return ok(
        RetentionPolicy(
            retention_days=tenant.recording_retention_days,
            default_days=settings.recording_retention_days_default,
        )
    )


@router.patch(
    "/tenant/config/retention",
    response_model=ResponseModel[RetentionPolicy],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def patch_retention_policy(
    body: RetentionPolicyUpdate,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    settings: AppSettings,
    audit: AuthAudit,
    caller: VerifiedIdentity = require("recordings:manage"),
) -> ResponseModel[RetentionPolicy]:
    tenant = await _load_tenant(session, tenant_id)
    old_days = tenant.recording_retention_days
    tenant.recording_retention_days = body.retention_days
    # Policy-change before/after (spec decision 5). Config values, not PHI.
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.RETENTION_POLICY_UPDATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"old_days": old_days, "new_days": body.retention_days},
    )
    return ok(
        RetentionPolicy(
            retention_days=body.retention_days,
            default_days=settings.recording_retention_days_default,
        )
    )
```

- [ ] **Step 4: CHECK-widening migration** — `just makemigration "widen auth audit event check for retention policy"`; copy the `0017_persona_tweak_event.py` shape exactly: drop `ck_auth_audit_log_event_type_valid`, re-add from `values_of(AuthEvent)`; `_OLD_VALUES` = the current tuple minus `"retention_policy_updated"` (read the latest widening migration — `20260708_1234_fb43bdd169b2_*` — and extend its value list).

- [ ] **Step 5: Verify** — `just test tests/unit/http/test_retention_policy.py -v` → PASS; `just migrate` clean.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(recording): tenant retention-policy endpoint with old/new audit"`

---

### Task 11: Playback endpoint — GET /calls/{call_id}/recording

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py`
- Modify: `apps/control_plane/src/control_plane/deps.py` (add `get_recording_storage`)
- Modify: `packages/vera_core/src/vera_core/schemas/dto.py` + `__init__.py` (`RecordingPlayback`)
- Test: `tests/unit/http/test_recording_playback.py`

**Interfaces:**
- Consumes: Task 4 `RecordingStorage.signed_url`/`parse_gcs_uri` (via `app.state.recording_storage`), Task 1 `RecordingStatus`/TTL setting, Task 2 `recordings:read`.
- Produces: `GET /api/v1/calls/{call_id}/recording` → `ResponseModel[RecordingPlayback]` where `RecordingPlayback(url: str, expires_at: datetime)`. Status contract: 404 call not found / not visible / no recording row; 409 recording exists but not `AVAILABLE` (or storage unconfigured); 200 with signed URL + `RECORDING_ACCESSED` audit.

- [ ] **Step 1: Write the failing test** — cases (reuse the `tests/unit/http/` fixtures; inject `InMemoryRecordingStorage` via `create_app(...)`'s state or monkeypatch `app.state.recording_storage`):

1. Owner + `AVAILABLE` recording → 200; body `url` startswith `https://storage.local/`; audit sink captured a record with `event_type == "recording.accessed"` and `detail["recording_id"]`.
2. Non-owner + unpublished call → 404 (no enumeration — same shape as absent call).
3. Non-owner + published call → 200 (visibility widened).
4. Non-owner in `revoked_user_ids` on a published call → 404.
5. Recording `PENDING` → 409.
6. No recording row → 404.
7. Caller without `recordings:read` → 403.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

`schemas/dto.py`:

```python
class RecordingPlayback(BaseModel):
    """A short-lived signed URL for one recording. The URL itself is the
    credential — never logged, never cached (Cache-Control: no-store)."""

    url: str
    expires_at: datetime
```

`deps.py` (match the module's existing getter style):

```python
def get_recording_storage(request: Request) -> "RecordingStorage":
    storage = request.app.state.recording_storage
    if storage is None:
        # Recording disabled platform-wide (no bucket configured).
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT, message="call recording is not configured"
        )
    return storage
```

`calls.py` — new endpoint (imports: `RecordingPlayback`, `Recording`, `RecordingStatus`, `get_recording_storage`, `parse_gcs_uri`, `AppSettings`, `from datetime import UTC, datetime, timedelta`):

```python
@router.get(
    "/calls/{call_id}/recording",
    response_model=ResponseModel[RecordingPlayback],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def get_recording_playback(
    call_id: UUID,
    request: Request,
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,
    settings: AppSettings,
    storage: RecordingStorageDep,
    caller: VerifiedIdentity = require("recordings:read"),
) -> ResponseModel[RecordingPlayback]:
    """Mint a TTL-bounded signed URL for the call's recording.

    Authorization is permission AND call visibility (spec decision 6): the
    recording is never more visible than the call itself. Every issuance is a
    PHI disclosure → RECORDING_ACCESSED on the append-only audit trail.
    """
    response.headers["Cache-Control"] = "no-store"
    call = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()
    if call is None:
        raise NotFoundError(message="call not found")
    if call.initiated_by_id != caller.user_id:
        # Same non-enumeration contract as join_token: revoked users and private
        # calls get the identical 404.
        revoked = str(caller.user_id) in call.revoked_user_ids
        if revoked or (call.initiated_by_id is not None and not call.published):
            raise NotFoundError(message="call not found")

    recording = (
        await session.execute(
            select(Recording)
            .where(Recording.call_id == call_id)
            .order_by(Recording.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if recording is None:
        raise NotFoundError(message="no recording for this call")
    if recording.status != RecordingStatus.AVAILABLE.value:
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message=f"recording is not available (status: {recording.status})",
        )

    bucket, object_path = parse_gcs_uri(recording.gcs_uri)
    ttl = settings.recording_signed_url_ttl_seconds
    url = await storage.signed_url(bucket, object_path, ttl_seconds=ttl)
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.RECORDING_ACCESSED.value,
            resource_type="recording",
            resource_id=str(recording.id),
            permission_key="recordings:read",
            decision="allow",
            request_id=current_request_id(request),
            detail={"call_id": str(call_id), "ttl_seconds": ttl},
        )
    )
    # expires_at is informational for the client; the URL's own signature is the
    # enforcement (GCS rejects after expiry regardless of this field).
    return ok(RecordingPlayback(url=url, expires_at=datetime.now(UTC) + timedelta(seconds=ttl)))
```

Add the dep alias in `api/v1/common.py`: `RecordingStorageDep = Annotated["RecordingStorage", Depends(get_recording_storage)]` (TYPE_CHECKING import of `RecordingStorage` from `control_plane.recording_storage`).

- [ ] **Step 4: Verify** — `just test tests/unit/http/test_recording_playback.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(recording): RBAC-gated playback endpoint with signed URL + access log"`

---

### Task 12: DevOps rows, boot verification, full gate, simplify

**Files:**
- Modify: `vera-backend/adr/devops-todo.md` (extend row 7's deferred entry; add signing + backstop rows)
- No code changes expected (fixes only).

- [ ] **Step 1: devops-todo rows** — replace/extend the existing recording row with three concrete obligations (match the file's existing table/row format):

1. Recordings bucket: CMEK-encrypted; LiveKit egress service SA → `roles/storage.objectCreator` (write-only); control-plane SA → `roles/storage.objectViewer` + `roles/storage.objectAdmin`-scoped delete (or objectUser) on this bucket only. Env: `VERA_RECORDING_BUCKET`/`VERA_RECORDING_PREFIX` on the control plane.
2. Signed URLs: control-plane SA needs `roles/iam.serviceAccountTokenCreator` on ITSELF (IAM signBlob under Workload Identity — no exported keys).
3. Bucket lifecycle rule as a backstop BEHIND the app-owned sweeper: delete at `max tenant retention + 30d`, so the audited sweeper always acts first and `audit_log` stays the authoritative destruction record.

- [ ] **Step 2: Boot verification (repo rule for background loops)** — with `just up && just migrate`, `LOCAL_KMS_MASTER_KEY` set, `VERA_LIVEKIT_URL` set, and `VERA_RECORDING_BUCKET=test-bucket` exported: run `just api` and watch ≥3 loop windows (~2 min):
  - worker-event consumer: idle ticks stay silent (no `TimeoutError` tracebacks, no back-off spam);
  - recording verifier: ticks every 30s without error (empty work list);
  - retention sweeper: first tick clean.
  Then `just worker` and confirm it registers. Any traceback on an idle window is a failure — fix before proceeding (this is exactly the Redis BLOCK-timeout footgun class).

- [ ] **Step 3: Full gate** — `just check` → all green (ruff + mypy --strict + pytest).

- [ ] **Step 4: Simplify (repo-mandatory)** — run the `/simplify` skill on the change set (all files touched by Tasks 1–11), then re-run `just check`.

- [ ] **Step 5: Final commit + PR**

```bash
git add -A && git commit -m "chore(recording): devops obligations + boot-verified background loops"
git push -u origin feat/call-recording-persistence
```

Open a PR to `main` titled `feat: call recording & persistence (egress → GCS, transcripts, retention, playback)` referencing the spec path in the body.

---

## Self-review notes (already applied)

- Spec coverage: egress start (T3+T5), pending row + sha256 verify (T5+T8), transcript persistence (T6+T7), retention policy + both snapshot kinds (T9 deletion before/after, T10 policy old/new), playback + RBAC + TTL signed URL + access log (T11), permissions (T2), infra rows (T12). Discard-on-no-answer (T8) uses `RECORDING_DISCARDED`, a deliberate small addition beyond the spec's five events (PHI-object deletion deserves its own evidence).
- Transcript grace-TTL durability: bumped to 900s in T6 — the finalizer's budget to survive a control-plane restart; `drain()` never blocks on a missing stream (unlike `collect()`, which would hang the consumer).
- Cross-tenant scans use SECURITY DEFINER work-list fns (ids only), mutations under `tenant_session` — matches the `audit_chain_heads` precedent; no RLS bypass for row data.
- Type consistency: `EgressState` (T3) consumed by T8; `RecordingConfig`/`start_recording_for_call` (T5) consumed by both call sites; `build_transcript_rows` explicit-id rows because executemany bypasses ORM client defaults.
