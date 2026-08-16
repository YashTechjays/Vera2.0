# Browser-Callee Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the complete production call lifecycle — enqueue, dispatch, compile plan, create room, answer, converse, close out — with the payer rep joining from the browser instead of over a SIP trunk, so voice-path changes can be verified without a working Twilio call.

**Architecture:** A default-off setting (`VERA_BROWSER_CALLEE_TRANSPORT`) rides on `LiveKitGateway`, which is already injected everywhere the decision must be made. The enqueue gate skips its trunk check, the dispatcher skips its trunk lookup and its `create_sip_participant` dial, and a new `?callee=true` join mode mints a publish-capable `caller-` token. The agent worker fires `call.answered` for a browser speaker when the dispatch metadata says browser-callee, which carries the call `INITIATED → ACTIVE` so everything downstream behaves exactly as it does in production.

**Tech Stack:** Python 3.12 (FastAPI, SQLAlchemy async, livekit-agents, pytest) + React/Vite/TypeScript (LiveKit client SDK, vitest).

**Spec:** `docs/superpowers/specs/2026-08-08-browser-callee-transport-design.md`

## Global Constraints

- **Backend gate:** `just check` (ruff check + ruff format --check + mypy --strict + pytest) must pass on the exact tree pushed. Run it verbatim — never a hand-picked subset.
- **Frontend gate:** all four of `npx tsc -b`, `npx eslint .`, `npm test`, `npm run build`, from `vera-frontend/`.
- **After implementation, before claiming done:** run the `/simplify` skill on the change, then re-run both gates.
- **Comments:** default to none. Add one only when it explains something the code cannot — a non-obvious constraint or a deliberate trade-off — and keep it to one line. Docstrings are a single sentence.
- **PHI:** never log, trace, or span raw transcript, `agent_context`, or `metadata` contents. Log exception *type names*, not tracebacks, around PHI-touching I/O.
- **Type style:** PEP 695 type params (`class Foo[T]`, `def f[T]`). Ruff rejects `Generic[T]` / `TypeVar`.
- **No migration in this plan.** `audit_log.event_type` is `String(64)`, so a new `AuditEvent` member needs no schema change. If you find yourself writing a migration, stop — you have gone off-plan.
- **Do not change `_SPEAKER_TIMEOUT_S`** (`agent_worker/main.py:81`). It stays at 60 seconds by explicit decision.
- **Do not touch** the CallPlan compile, prefill fuse, `plan_service.put`, `create_call_room`, or the `INITIATED` `CallEvent` in `try_dispatch`. That path must stay byte-identical — it is the code under test.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `vera-backend/packages/vera_core/src/vera_core/config/settings.py` | declares the flag | 1 |
| `vera-backend/apps/control_plane/src/control_plane/livekit_gateway.py` | carries the flag on the gateway | 1 |
| `vera-backend/apps/control_plane/src/control_plane/queueability.py` | skips the trunk precondition | 2 |
| `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py` | passes the flag to the enqueue gate | 2 |
| `vera-backend/packages/vera_core/src/vera_core/services/queue_dispatcher.py` | skips trunk lookup + dial; sets `browser_callee` metadata | 3 |
| `vera-backend/packages/vera_core/src/vera_core/models/audit_log.py` | `CALL_CALLEE_JOIN` event | 4 |
| `vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py` | the `callee=true` join mode | 4 |
| `vera-backend/apps/agent_worker/src/agent_worker/main.py` | `should_emit_answered` + its use | 5 |
| `vera-frontend/src/lib/monitoring/liveCallView.ts` | `LiveCallMode` third member, `caller-` → callee badge | 6 |
| `vera-frontend/src/lib/api/calls.ts` | `getJoinToken` mode parameter | 6 |
| `vera-frontend/src/components/monitoring/LiveCallRoom.tsx` | `mode` prop replaces `microphone` | 7 |
| `vera-frontend/src/components/monitoring/LiveCallModal.tsx` | the "Join as payer rep" button | 7 |

Tasks 1–5 are backend and land in order; after Task 5 the feature is fully usable via `curl` for the join token. Tasks 6–7 add the UI.

---

### Task 1: The flag, carried on the gateway

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/config/settings.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/livekit_gateway.py:97-122, 351-359`
- Test: `vera-backend/tests/unit/control_plane/test_livekit_gateway_egress.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Settings.browser_callee_transport: bool` (default `False`, env `VERA_BROWSER_CALLEE_TRANSPORT`)
  - `LiveKitGateway.__init__(..., browser_callee_transport: bool = False)`
  - `LiveKitGateway.browser_callee_transport -> bool` (read-only property)

- [ ] **Step 1: Write the failing test**

Append to `vera-backend/tests/unit/control_plane/test_livekit_gateway_egress.py`:

```python
def test_gateway_defaults_to_sip_transport() -> None:
    gw = LiveKitGateway(url="ws://x", api_key="k", api_secret="s")
    assert gw.browser_callee_transport is False


def test_build_gateway_carries_browser_callee_transport() -> None:
    settings = Settings(
        livekit_url="ws://x",
        browser_callee_transport=True,
    )
    secrets = _StubSecrets({"LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s"})
    assert build_livekit_gateway(settings, secrets).browser_callee_transport is True
```

Add whatever imports that file is missing (`LiveKitGateway`, `build_livekit_gateway`, `Settings`). If the file has no secrets stub, add one:

```python
class _StubSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, key: str) -> str:
        return self._values[key]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane/test_livekit_gateway_egress.py -k browser_callee -v`
Expected: FAIL — `AttributeError: 'LiveKitGateway' object has no attribute 'browser_callee_transport'`

- [ ] **Step 3: Add the setting**

In `settings.py`, next to the other LiveKit fields:

```python
    # Test transport: skip SIP entirely and let a browser participant join as the
    # payer rep. Never enable outside local/dev — see docs/superpowers/specs/
    # 2026-08-08-browser-callee-transport-design.md.
    browser_callee_transport: bool = False  # VERA_BROWSER_CALLEE_TRANSPORT
```

- [ ] **Step 4: Carry it on the gateway**

In `livekit_gateway.py`, extend `__init__` and add the property:

```python
    def __init__(
        self,
        url: str,
        api_key: str,
        api_secret: str,
        agent_name: str = AGENT_NAME,
        browser_callee_transport: bool = False,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret
        self._agent_name = agent_name
        self._browser_callee_transport = browser_callee_transport

    @property
    def url(self) -> str:
        return self._url

    @property
    def browser_callee_transport(self) -> bool:
        """This deployment places no SIP call — the callee joins from a browser."""
        return self._browser_callee_transport
```

And in `build_livekit_gateway`:

```python
    return LiveKitGateway(
        url=settings.livekit_url,
        api_key=secrets.get("LIVEKIT_API_KEY"),
        api_secret=secrets.get("LIVEKIT_API_SECRET"),
        agent_name=settings.livekit_agent_name,
        browser_callee_transport=settings.browser_callee_transport,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane/test_livekit_gateway_egress.py -k browser_callee -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Document the env var**

Add to `vera-backend/env.example`, under the LiveKit section:

```
# Test transport only — skip SIP and let the payer rep join from the browser.
# Leave unset/false everywhere except local dev.
VERA_BROWSER_CALLEE_TRANSPORT=false
```

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/config/settings.py \
        vera-backend/apps/control_plane/src/control_plane/livekit_gateway.py \
        vera-backend/tests/unit/control_plane/test_livekit_gateway_egress.py \
        vera-backend/env.example
git commit -m "feat(livekit): carry a browser-callee transport flag on the gateway"
```

---

### Task 2: Enqueue gate skips the trunk check

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/queueability.py:31-47`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py:1427`
- Test: `vera-backend/tests/unit/control_plane/test_queueability.py`

**Interfaces:**
- Consumes: `LiveKitGateway.browser_callee_transport` (Task 1).
- Produces: `ensure_queueable(session, kms, form, *, browser_callee: bool = False) -> None`

The keyword is optional so every existing positional call site and test keeps working.

- [ ] **Step 1: Write the failing tests**

Append to `vera-backend/tests/unit/control_plane/test_queueability.py`:

```python
@pytest.mark.asyncio
async def test_browser_callee_allows_a_missing_trunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("control_plane.queueability.get_integration_credentials", _creds_missing)
    await ensure_queueable(
        cast(AsyncSession, _FakeSession()),
        cast(KeyManagementService, object()),
        _form("+15551234567"),
        browser_callee=True,
    )


@pytest.mark.asyncio
async def test_browser_callee_still_requires_e164(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("control_plane.queueability.get_integration_credentials", _creds_missing)
    with pytest.raises(CustomAPIException) as exc:
        await ensure_queueable(
            cast(AsyncSession, _FakeSession()),
            cast(KeyManagementService, object()),
            _form("555-1234"),
            browser_callee=True,
        )
    assert "phone" in str(exc.value.message).lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane/test_queueability.py -k browser_callee -v`
Expected: FAIL — `TypeError: ensure_queueable() got an unexpected keyword argument 'browser_callee'`

- [ ] **Step 3: Implement**

Replace `ensure_queueable` in `queueability.py`:

```python
async def ensure_queueable(
    session: "AsyncSession",
    kms: "KeyManagementService",
    form: "PatientForm",
    *,
    browser_callee: bool = False,
) -> None:
    """Raise if *form* cannot possibly be dialed once dispatched."""
    phone = form.insurance_provider_phone_number
    if not phone or not E164_RE.match(phone):
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message="form has no valid insurance provider phone number (E.164 required)",
            data={"field": "insurance_provider_phone_number"},
        )
    if browser_callee:
        return
    creds = await get_integration_credentials(session, kms, integration_type_name=TRUNK_INTEGRATION)
    if not (creds or {}).get("trunk_id"):
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message="outbound calling is not configured for this tenant (missing SIP trunk)",
        )
```

Also update the module docstring's first sentence to name the exception:

```python
"""Enqueue-time gates for `PUT /patient-forms/{id}/status` → IN_QUEUE, run BEFORE the
state-machine transition: `ensure_queueable` rejects a form that could never be dialed
(no payer phone, no outbound trunk — the trunk half is skipped under browser-callee
transport); `ensure_va_capacity` rejects an enqueue that would put the caller past the
tenant's per-VA in-flight limit. Working hours stay dial-time concerns the dispatcher
handles.
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane/test_queueability.py -v`
Expected: PASS — the two new tests plus every pre-existing one.

- [ ] **Step 5: Wire the call site**

In `patient_forms.py:1427`, `livekit` is already a parameter of this endpoint (it is passed to `schedule_dispatch_pass` at :1515). Change:

```python
        await ensure_queueable(session, kms, form, browser_callee=livekit.browser_callee_transport)
```

- [ ] **Step 6: Verify nothing else regressed**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane tests/integration/control_plane/test_call_queue.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/queueability.py \
        vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        vera-backend/tests/unit/control_plane/test_queueability.py
git commit -m "feat(queue): let a form enqueue without a SIP trunk under browser-callee transport"
```

---

### Task 3: Dispatcher skips the trunk lookup and the dial

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/services/queue_dispatcher.py:306-321, 364-368, 528-562`
- Test: `vera-backend/tests/unit/services/test_queue_dispatcher.py`

**Interfaces:**
- Consumes: `LiveKitGateway.browser_callee_transport` (Task 1).
- Produces: dispatch room metadata gains `"browser_callee": True` under this transport — the key Task 5's worker reads.

`try_dispatch` types its gateway as `Any` (duck-typed so tests can pass fakes), so read the flag with `getattr(..., False)`: a gateway that does not declare it is a SIP gateway.

- [ ] **Step 1: Write the failing test**

Append to `vera-backend/tests/unit/services/test_queue_dispatcher.py`. Reuse that module's existing `FakeLiveKit`, `FakeSession`, `_tenant`, `_form`, and `_stub_credentials` fixtures exactly as the neighbouring tests do:

```python
@pytest.mark.asyncio
async def test_browser_callee_dispatches_without_a_trunk_and_never_dials(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    """Under browser-callee transport the pass creates the room and counts the call
    dispatched even with no trunk configured, and places no SIP call."""
    _stub_credentials["value"] = None  # no trunk — would blank candidates on the SIP path
    tenant = _tenant()
    form = _form(tenant.id)
    livekit = FakeLiveKit()
    livekit.browser_callee_transport = True

    dispatched = await try_dispatch(
        cast(AsyncSession, FakeSession(tenant=tenant, candidates=[form])),
        tenant.id,
        livekit,
        object(),
    )

    assert dispatched == 1
    assert len(livekit.created) == 1
    assert livekit.sip_dials == []
    assert livekit.created_metadata[0]["browser_callee"] is True


@pytest.mark.asyncio
async def test_sip_transport_still_needs_a_trunk(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    """The default path is unchanged: no trunk, nothing dispatched."""
    _stub_credentials["value"] = None
    tenant = _tenant()
    form = _form(tenant.id)
    livekit = FakeLiveKit()

    dispatched = await try_dispatch(
        cast(AsyncSession, FakeSession(tenant=tenant, candidates=[form])),
        tenant.id,
        livekit,
        object(),
    )

    assert dispatched == 0
    assert livekit.created == []
```

Then extend `FakeLiveKit` (`test_queue_dispatcher.py:233`) so it can stand in for both transports and so metadata is assertable:

```python
class FakeLiveKit:
    """Minimal LiveKitGateway stand-in — records room creation + SIP dials."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.created_metadata: list[dict[str, object]] = []
        self.browser_callee_transport = False
        ...  # leave the rest of __init__ exactly as it is

    async def create_call_room(
        self, room_name: str, metadata: dict[str, object] | None = None
    ) -> None:
        if self.room_error is not None:
            raise self.room_error
        self.created.append(room_name)
        self.created_metadata.append(metadata or {})
```

If `FakeLiveKit.create_call_room` already records metadata under a different attribute name, use that one and adjust the assertion instead of adding a second list.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/unit/services/test_queue_dispatcher.py -k "browser_callee or still_needs_a_trunk" -v`
Expected: FAIL — the browser-callee case asserts `dispatched == 1` but gets `0` (no trunk blanked the candidates).

- [ ] **Step 3: Skip the trunk lookup**

Replace the trunk-resolution block at `queue_dispatcher.py:306-321`:

```python
    # Resolve the tenant's outbound SIP trunk once for the whole pass — every
    # candidate dials through the same trunk. Skip the lookup entirely when
    # there's nothing to dispatch, or when no SIP call will be placed at all.
    # getattr, not attribute access: the gateway is duck-typed here, and a fake
    # without the attribute is a SIP gateway.
    browser_callee: bool = getattr(livekit, "browser_callee_transport", False)
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
```

- [ ] **Step 4: Add the metadata key**

In the per-form metadata block at `queue_dispatcher.py:364-368`, after the dict literal:

```python
        if browser_callee:
            # Tells the worker a browser speaker stands in for an answered SIP callee.
            metadata["browser_callee"] = True
```

- [ ] **Step 5: Skip the dial**

Wrap the dial block at `queue_dispatcher.py:528-562`. The `if dial_attempted` pacing sleep, the `dial_attempted = True` assignment, and the whole `try/except OutboundDialError` move inside:

```python
        # 4d. Dial OUTSIDE the savepoint: a failed dial keeps the Call row as
        # evidence (FAILED + retry accounting) instead of rolling it back. Pace
        # every dial attempt ~1/s (carrier CPS limit) — failed dials still consume
        # carrier capacity — sleep between attempts, never before the first.
        # Under browser-callee transport there is no dial at all: the room waits
        # for a browser participant instead.
        if not browser_callee:
            if dial_attempted:
                await asyncio.sleep(dial_pacing_s)
            dial_attempted = True
            try:
                await livekit.create_sip_participant(
                    room_name, form.insurance_provider_phone_number, trunk_id
                )
            except OutboundDialError as exc:
                # str(exc), not .diagnostic: keeps the detail when there is no code to render.
                logger.warning("dispatch: outbound dial failed for call %s: %s", call.id, exc)
                with contextlib.suppress(Exception):  # room teardown is best-effort
                    await livekit.delete_room(room_name)
                requeued = apply_terminal_call_status(
                    call,
                    form,
                    CallStatus.FAILED,
                    tenant_max_retries=tenant.max_retries,
                    auto_retry_enabled=tenant.auto_retry_enabled,
                )
                call.ended_at = func.now()
                if requeued:
                    form.enqueued_at = func.now()
                session.add(
                    CallEvent(
                        tenant_id=tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.STATUS.value,
                        event_value=CallStatus.FAILED.value,
                    )
                )
                continue
```

Do not change anything below this block — `dispatched += 1`, the recording start, the log line, and the audit emit all run unchanged.

`trunk_id` is `None` under browser-callee transport but is now only read inside this branch, so mypy stays happy without an assert.

- [ ] **Step 6: Update the module docstring**

The first paragraph of `queue_dispatcher.py` says the dispatcher "sleeps between dials for carrier pacing". Add one clause so the file's own header is not lying:

```python
"""Event-driven call queue dispatcher.

Pulls admitted forms from the tenant's queue, checks concurrency limits and
insurance-provider working hours, and initiates calls. Invoked on two events:
(1) a form is enqueued, (2) a call ends and a concurrency slot frees up. Under
browser-callee transport (a test-only gateway flag) no SIP call is placed and the
room simply waits for a browser participant.
```

Leave the rest of the docstring untouched.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/services/test_queue_dispatcher.py -v`
Expected: PASS — both new tests plus every pre-existing dispatcher test (the SIP path must be untouched).

- [ ] **Step 8: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/services/queue_dispatcher.py \
        vera-backend/tests/unit/services/test_queue_dispatcher.py
git commit -m "feat(dispatch): skip the SIP dial under browser-callee transport"
```

---

### Task 4: The `callee=true` join token

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/audit_log.py:52-55`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py:265-373`
- Test: `vera-backend/tests/integration/control_plane/test_calls.py`

**Interfaces:**
- Consumes: `LiveKitGateway.browser_callee_transport` (Task 1), `mint_join_token` (existing).
- Produces:
  - `GET /calls/{call_id}/join-token?callee=true` → `JoinTokenResponse`, identity `caller-{user_id}`, `can_publish=True`, **no** `vera.mode` attribute
  - `AuditEvent.CALL_CALLEE_JOIN = "call.callee.join"`

Three things must NOT happen on this path: no `vera.mode` attribute (any value trips `AgentTakeoverController` and permanently silences the agent), no intervener-lock write, no `InterventionEvent` row.

- [ ] **Step 1: Write the failing tests**

Append to `vera-backend/tests/integration/control_plane/test_calls.py`, following that module's existing client/auth fixture style for join-token tests:

```python
@pytest.mark.asyncio
async def test_callee_join_token_is_publishable_and_unbadged(
    client: AsyncClient, va_headers: dict[str, str], live_call: Call, app: FastAPI
) -> None:
    """A callee token publishes, carries a caller- identity, and has NO vera.mode —
    any mode attribute would trip the worker's takeover controller and mute the agent."""
    app.state.livekit.browser_callee_transport = True

    res = await client.get(
        f"/api/v1/calls/{live_call.id}/join-token?callee=true", headers=va_headers
    )

    assert res.status_code == 200
    minted = app.state.livekit.minted[-1]
    assert minted["identity"].startswith("caller-")
    assert minted["can_publish"] is True
    assert not minted["attributes"]


@pytest.mark.asyncio
async def test_callee_join_token_claims_no_intervener_lock(
    client: AsyncClient,
    va_headers: dict[str, str],
    live_call: Call,
    app: FastAPI,
    session: AsyncSession,
) -> None:
    app.state.livekit.browser_callee_transport = True

    res = await client.get(
        f"/api/v1/calls/{live_call.id}/join-token?callee=true", headers=va_headers
    )

    assert res.status_code == 200
    await session.refresh(live_call)
    assert live_call.intervener_user_id is None


@pytest.mark.asyncio
async def test_callee_join_token_refused_when_transport_is_off(
    client: AsyncClient, va_headers: dict[str, str], live_call: Call, app: FastAPI
) -> None:
    app.state.livekit.browser_callee_transport = False

    res = await client.get(
        f"/api/v1/calls/{live_call.id}/join-token?callee=true", headers=va_headers
    )

    assert res.status_code == 409


@pytest.mark.asyncio
async def test_callee_and_intervene_are_mutually_exclusive(
    client: AsyncClient, va_headers: dict[str, str], live_call: Call, app: FastAPI
) -> None:
    app.state.livekit.browser_callee_transport = True

    res = await client.get(
        f"/api/v1/calls/{live_call.id}/join-token?callee=true&intervene=true",
        headers=va_headers,
    )

    assert res.status_code == 422
```

Adapt the fixture names (`client`, `va_headers`, `live_call`, `app`, `session`) to whatever the surrounding join-token tests in that file already use — do not invent new fixtures. If the file's LiveKit stub does not record minted tokens, extend it to append `{"identity", "can_publish", "attributes"}` dicts to a `minted` list, and set `browser_callee_transport = False` in its `__init__`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_calls.py -k callee -v`
Expected: FAIL — `callee=true` is an unknown query parameter, so the endpoint mints a listen-only `supervisor-` token and the identity assertion fails.

- [ ] **Step 3: Add the audit event**

In `vera_core/models/audit_log.py`, directly after `CALL_INTERVENE_JOIN`:

```python
    # A publish-capable join as the CALLEE (browser-callee test transport) — the
    # participant the outbound SIP call would have reached. Not a supervisor: it
    # claims no intervener lock and carries no vera.mode.
    CALL_CALLEE_JOIN = "call.callee.join"
```

- [ ] **Step 4: Implement the join mode**

In `api/v1/calls.py`, add the import:

```python
from vera_core.observability.correlation import CALLER_IDENTITY_PREFIX
```

(alongside the existing `correlation` imports — `supervisor_identity`, `PARTICIPANT_MODE_ATTR`, etc.)

Add the parameter to `join_token`, beside `intervene: bool = False`:

```python
    callee: bool = False,
```

Immediately after `response.headers["Cache-Control"] = "no-store"`, add the two gates:

```python
    if callee and intervene:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message="callee and intervene are mutually exclusive join modes",
        )
    if callee and not livekit.browser_callee_transport:
        raise ConflictError(message="browser-callee transport is not enabled")
```

Replace the identity assignment:

```python
    identity = (
        f"{CALLER_IDENTITY_PREFIX}{caller.user_id}"
        if callee
        else supervisor_identity(caller.user_id, caller.session_id)
    )
```

In the existing `AuditRecord(...)` construction, extend the `event_type` and `permission_key` expressions — do not add a second record:

```python
            event_type=(
                AuditEvent.CALL_CALLEE_JOIN
                if callee
                else AuditEvent.CALL_INTERVENE_JOIN
                if intervene
                else AuditEvent.CALL_LISTEN_ONLY_JOIN
            ).value,
            ...
            permission_key="calls:intervene" if intervene else "calls:read",
```

Replace the `mint_join_token` call:

```python
    # Watch-only tokens are server-side mute; intervene and callee may publish. The
    # callee carries NO vera.mode: any value trips the worker's takeover controller
    # and permanently silences the agent.
    token = livekit.mint_join_token(
        room_name=room_name,
        identity=identity,
        can_publish=intervene or callee,
        name=caller.email or caller.subject,
        attributes=(
            None
            if callee
            else {
                PARTICIPANT_MODE_ATTR: (
                    PARTICIPANT_MODE_INTERVENER if intervene else PARTICIPANT_MODE_LISTENER
                )
            }
        ),
        # Cap the intervene token at the grace so a stale token can't outlive a stolen lock.
        ttl=_INTERVENE_CONNECT_GRACE if intervene else _LISTEN_TOKEN_TTL,
    )
```

The `if intervene:` lock-claim block is untouched — `callee` never enters it.

- [ ] **Step 5: Update the module docstring**

`calls.py:1-8` describes join-token as listen-only-or-intervene. Add one sentence:

```python
"""Verification-call endpoints: join-token, active-list, live event stream,
...
(`?intervene=true`) additionally requires `calls:intervene`, checked after the
visibility 404s, and claims the call's single-intervener lock. `?callee=true` is the
test-only browser-callee join: a publish-capable `caller-` participant standing in for
the SIP callee, claiming no lock — refused unless the gateway enables that transport.
"""
```

Keep the existing wording; only append the new sentence.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_calls.py -v`
Expected: PASS — the four new tests plus every pre-existing join-token test (listen-only and intervene must be unchanged).

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/models/audit_log.py \
        vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py \
        vera-backend/tests/integration/control_plane/test_calls.py
git commit -m "feat(calls): mint a publish-capable callee join token under browser transport"
```

---

### Task 5: The worker treats a browser callee as answered

**Files:**
- Modify: `vera-backend/apps/agent_worker/src/agent_worker/main.py` (new helper near `_is_ready_speaker` at :93; use it at :397-399)
- Test: `vera-backend/tests/unit/worker/test_wait_for_speaker.py`

**Interfaces:**
- Consumes: the `"browser_callee"` metadata key set in Task 3.
- Produces: `should_emit_answered(speaker: rtc.Participant, meta: dict[str, Any]) -> bool`

Extracted as a pure helper because `entrypoint` is not unit-testable.

- [ ] **Step 1: Write the failing tests**

Append to `vera-backend/tests/unit/worker/test_wait_for_speaker.py` (it already defines `_FakeParticipant`, `_SIP`, and `_STANDARD`):

```python
def test_sip_callee_is_always_answered() -> None:
    assert should_emit_answered(_FakeParticipant("phone-callee", kind=_SIP), {}) is True


def test_browser_speaker_is_not_answered_by_default() -> None:
    """Voice Lab browser mode: a browser caller is not an answered phone call."""
    assert should_emit_answered(_FakeParticipant("caller-abc"), {}) is False


def test_browser_speaker_is_answered_under_browser_callee_transport() -> None:
    assert (
        should_emit_answered(_FakeParticipant("caller-abc"), {"browser_callee": True}) is True
    )
```

Add `should_emit_answered` to the existing `from agent_worker.main import (...)` block.

The `_FakeParticipant` calls pass `cast`-free objects into a `rtc.Participant` parameter; if mypy --strict objects, wrap them as the neighbouring tests in this file already do.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/unit/worker/test_wait_for_speaker.py -k answered -v`
Expected: FAIL — `ImportError: cannot import name 'should_emit_answered' from 'agent_worker.main'`

- [ ] **Step 3: Write the helper**

In `agent_worker/main.py`, immediately after `_is_ready_speaker`:

```python
def should_emit_answered(speaker: rtc.Participant, meta: dict[str, Any]) -> bool:
    """True when this speaker's presence means the call is live. A SIP callee answering
    is the real signal; under browser-callee transport a browser speaker stands in for it
    (a Voice Lab browser caller, which sets no such metadata, still does not)."""
    if speaker.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        return True
    return bool(meta.get("browser_callee"))
```

Add `from typing import Any` to the imports if it is not already there.

- [ ] **Step 4: Use it**

At `main.py:397-399`, replace the SIP-kind condition:

```python
            speaker = outcome.participant
            # The SIP callee answering is the "call is live" signal; under browser-callee
            # transport the browser speaker stands in for it.
            if lifecycle is not None and should_emit_answered(speaker, meta):
                await lifecycle.answered(now_ms=int(time.time() * 1000))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/worker/ -v`
Expected: PASS — the three new tests plus every pre-existing worker test.

- [ ] **Step 6: Run the full backend gate**

Run: `cd vera-backend && just check`
Expected: PASS (ruff check, ruff format --check, mypy --strict, pytest all green)

- [ ] **Step 7: Commit**

```bash
git add vera-backend/apps/agent_worker/src/agent_worker/main.py \
        vera-backend/tests/unit/worker/test_wait_for_speaker.py
git commit -m "feat(worker): treat a browser callee as an answered call"
```

---

### Task 6: Frontend view logic and API client

**Files:**
- Modify: `vera-frontend/src/lib/monitoring/liveCallView.ts:33-38, 65-72, 107`
- Modify: `vera-frontend/src/lib/api/calls.ts:142-147`
- Test: `vera-frontend/src/lib/monitoring/liveCallView.test.ts`
- Test: `vera-frontend/src/lib/api/calls.test.ts`

**Interfaces:**
- Consumes: `GET /calls/{id}/join-token?callee=true` (Task 4).
- Produces:
  - `type LiveCallMode = "listen" | "intervene" | "callee"`
  - `getJoinToken(callId: string, mode: LiveCallMode = "listen"): Promise<JoinTokenResponse>`
  - `participantMode()` maps a `caller-` identity to `"callee"` (badge "Insurance Rep")

`getJoinToken`'s second parameter changes from `intervene: boolean` to a mode string. Two existing tests in `calls.test.ts` pass `true` and must be updated to `"intervene"`.

- [ ] **Step 1: Write the failing tests**

Append to `vera-frontend/src/lib/monitoring/liveCallView.test.ts`:

```ts
describe("browser-callee participant", () => {
  it("labels a caller- identity as the insurance rep", () => {
    expect(participantMode({ identity: "caller-abc" })).toBe("callee")
    expect(participantLabel({ identity: "caller-abc" })).toBe("Insurance Rep")
  })

  it("still labels the SIP callee as the insurance rep", () => {
    expect(participantMode({ identity: "phone-callee" })).toBe("callee")
  })

  it("keeps supervisors and monitors as listeners", () => {
    expect(participantMode({ identity: "supervisor-abc~1" })).toBe("listener")
    expect(participantMode({ identity: "monitor-abc" })).toBe("listener")
  })

  it("does not close-lock the modal in callee mode", () => {
    expect(shouldAllowClose("callee", false, false)).toBe(true)
  })
})
```

Add `participantLabel` and `shouldAllowClose` to that file's imports if missing.

In `vera-frontend/src/lib/api/calls.test.ts`, change the two existing `getJoinToken("c1", true)` calls to `getJoinToken("c1", "intervene")`, and add:

```ts
  it("requests a callee token", async () => {
    vi.mocked(apiRequest).mockResolvedValue(joinToken)
    await getJoinToken("c1", "callee")
    expect(apiRequest).toHaveBeenCalledWith("/calls/c1/join-token?callee=true")
  })
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-frontend && npx vitest run src/lib/monitoring/liveCallView.test.ts src/lib/api/calls.test.ts`
Expected: FAIL — `participantMode({identity: "caller-abc"})` returns `"listener"`, and `getJoinToken("c1", "callee")` builds no query string.

- [ ] **Step 3: Implement the view logic**

In `liveCallView.ts`, drop `caller-` from the human-identity fallback list and give it its own branch:

```ts
// Mirrors backend vocabulary (vera_core.observability.correlation): the vera.mode attr + supervisor-/monitor-/caller- identity prefixes.
export const MODE_ATTR = "vera.mode"
const LISTENER_IDENTITY_PREFIXES = ["supervisor-", "monitor-"]
const CALLER_IDENTITY_PREFIX = "caller-"
const SIP_CALLEE_IDENTITY = "phone-callee"
```

```ts
/** Kind beats identity beats attribute; an unknown identity falls back to agent (self-hosted workers may lack ParticipantKind.Agent). */
export function participantMode(p: ParticipantLike): ParticipantMode {
  if (p.isAgent) return "agent"
  if (p.identity === SIP_CALLEE_IDENTITY) return "callee"
  // A caller- participant is the rep, over SIP or from a browser under the test transport.
  if (p.identity.startsWith(CALLER_IDENTITY_PREFIX)) return "callee"
  const mode = p.attributes?.[MODE_ATTR]
  if (mode === "intervener" || mode === "listener") return mode
  if (LISTENER_IDENTITY_PREFIXES.some((prefix) => p.identity.startsWith(prefix))) return "listener"
  return "agent"
}
```

Widen the mode union:

```ts
export type LiveCallMode = "listen" | "intervene" | "callee"
```

`shouldAllowClose` already reads `mode !== "intervene"`, so `"callee"` allows closing — which is what we want: closing the tab hangs up, exactly as a phone hangup does. No change needed there.

- [ ] **Step 4: Implement the API client**

In `calls.ts`:

```ts
/** GET /calls/{id}/join-token — listen-only by default; intervene mints a publish token
 *  (needs calls:intervene; claims the single-intervener lock, 409 while another holds it);
 *  callee mints a publish token as the payer rep (browser-callee test transport only, 409 off). */
export function getJoinToken(
  callId: string,
  mode: LiveCallMode = "listen",
): Promise<JoinTokenResponse> {
  const query = mode === "listen" ? "" : `?${mode === "intervene" ? "intervene" : "callee"}=true`
  return apiRequest<JoinTokenResponse>(`/calls/${encodeURIComponent(callId)}/join-token${query}`)
}
```

Add the import: `import type { LiveCallMode } from "@/lib/monitoring/liveCallView"` (match the path alias the file already uses for other imports).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd vera-frontend && npx vitest run src/lib/monitoring/liveCallView.test.ts src/lib/api/calls.test.ts`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add vera-frontend/src/lib/monitoring/liveCallView.ts \
        vera-frontend/src/lib/monitoring/liveCallView.test.ts \
        vera-frontend/src/lib/api/calls.ts \
        vera-frontend/src/lib/api/calls.test.ts
git commit -m "feat(monitoring): add a callee join mode to the view logic and API client"
```

---

### Task 7: The "Join as payer rep" button

**Files:**
- Modify: `vera-frontend/src/components/monitoring/LiveCallRoom.tsx:265-300, 356-378`
- Modify: `vera-frontend/src/components/monitoring/LiveCallModal.tsx:152, 260-265, 421-430, 488-500`
- Test: `vera-frontend/src/components/monitoring/LiveCallRoom.test.tsx`

**Interfaces:**
- Consumes: `LiveCallMode`, `getJoinToken(callId, mode)` (Task 6).
- Produces: `LiveCallRoom`'s `microphone?: boolean` prop is replaced by `mode?: LiveCallMode` (default `"listen"`).

`LiveCallRoom` needs the full mode, not a boolean, because the mode selects which token to mint. Internally `microphone` is derived.

- [ ] **Step 1: Write the failing test**

Append to `vera-frontend/src/components/monitoring/LiveCallRoom.test.tsx`. That file already mocks `getJoinToken`; change its mock to a spy so the argument is assertable:

```ts
const getJoinToken = vi.fn(() => Promise.resolve({ url: "ws://fake", token: "t" }))
```

(wire it into the existing `vi.mock` factory for the calls API module, replacing the inline arrow)

```tsx
it("requests a callee token in callee mode", async () => {
  render(<LiveCallRoom callId="c1" mode="callee" />)
  await waitFor(() => expect(getJoinToken).toHaveBeenCalledWith("c1", "callee"))
})

it("requests a listen token by default", async () => {
  render(<LiveCallRoom callId="c1" />)
  await waitFor(() => expect(getJoinToken).toHaveBeenCalledWith("c1", "listen"))
})
```

Import `waitFor` from `@testing-library/react` if it is not already imported.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-frontend && npx vitest run src/components/monitoring/LiveCallRoom.test.tsx`
Expected: FAIL — `LiveCallRoom` has no `mode` prop and calls `getJoinToken("c1", false)`.

- [ ] **Step 3: Swap the prop**

In `LiveCallRoom.tsx`, change the signature and derive `microphone`:

```tsx
export function LiveCallRoom({
  callId,
  mode = "listen",
  ended = false,
  endedStatus = null,
  onStatus,
  onJoinFailed,
}: {
  callId: string
  /** Publish the local mic. "intervene" = a supervisor speaking over the agent; "callee" = the
   *  browser standing in for the payer rep (test transport). A viewer must never be audible,
   *  and getUserMedia may be blocked (e.g. incognito). */
  mode?: LiveCallMode
  ...  // the remaining props are unchanged
}) {
  const microphone = mode !== "listen"
```

Update the effect's fetch and dependency array:

```tsx
    getJoinToken(callId, mode)
```
```tsx
  }, [callId, mode, ended, reconnectNonce])
```

Everything below — `handleRoomError`, `reconnect`, `<LiveKitRoom audio={microphone}>`, `<RoomView microphone={microphone} …>` — keeps using the derived `microphone` and needs no other change. Update the JSDoc above the component:

```tsx
/**
 * Joins a call's LiveKit room via a server-minted token and renders the live panel.
 *
 * Changing `mode` needs a new token, and LiveKit ignores a token swap while
 * connected — the parent must remount (key on the mode) to switch.
 */
```

Import `LiveCallMode` from `@/lib/monitoring/liveCallView`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-frontend && npx vitest run src/components/monitoring/LiveCallRoom.test.tsx`
Expected: PASS

- [ ] **Step 5: Add the button to the modal**

In `LiveCallModal.tsx`, add the env-var constant at module scope, beside the other imports:

```tsx
// Test transport only — must match the backend's VERA_BROWSER_CALLEE_TRANSPORT. The
// backend is the authority; this only decides whether the button renders.
const BROWSER_CALLEE = import.meta.env.VITE_BROWSER_CALLEE_TRANSPORT === "true"
```

Change the `LiveCallRoom` usage at :421-430:

```tsx
                <LiveCallRoom
                  key={`${call.id}:${mode}`}
                  callId={call.id}
                  mode={mode}
                  ended={sseEnded}
                  endedStatus={terminalStatus}
                  onStatus={setRoomStatus}
                  onJoinFailed={handleJoinFailed}
                />
```

Widen `handleJoinFailed` so a refused callee token also falls back to listening:

```tsx
  // A publish token can be refused (409 if someone took the mic, or the test transport
  // is off) — fall back to listening.
  function handleJoinFailed(error: unknown) {
    if (mode === "listen") return
    const what = mode === "callee" ? "join as the payer rep" : "intervene"
    setMode("listen")
    setActionError(error instanceof ApiError ? error.message : `Could not ${what}.`)
  }
```

Add the button in the footer, immediately before the existing Intervene block at :488:

```tsx
          {BROWSER_CALLEE && mode === "listen" && !callEnded && (
            <Button
              onClick={() => {
                setActionError(null)
                setMode("callee")
              }}
              disabled={roomStatus?.phase !== "live"}
              className="bg-sky-600 text-white hover:bg-sky-700"
            >
              Join as payer rep
            </Button>
          )}
```

The footer's right-hand cell currently holds only the Intervene button. Wrap both in a `<div className="flex items-center gap-2">` so they sit side by side rather than fighting for the same grid slot.

- [ ] **Step 6: Run the full frontend gate**

Run: `cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build`
Expected: all four PASS

- [ ] **Step 7: Document the frontend env var**

Add to `vera-frontend/.env.example` (create it if absent, matching the `VITE_DEV_EMAIL` style already used in `pages/Login.tsx`):

```
# Test transport only — shows "Join as payer rep" in Live Monitoring. Must match the
# backend's VERA_BROWSER_CALLEE_TRANSPORT.
VITE_BROWSER_CALLEE_TRANSPORT=false
```

- [ ] **Step 8: Commit**

```bash
git add vera-frontend/src/components/monitoring/LiveCallRoom.tsx \
        vera-frontend/src/components/monitoring/LiveCallRoom.test.tsx \
        vera-frontend/src/components/monitoring/LiveCallModal.tsx \
        vera-frontend/.env.example
git commit -m "feat(monitoring): add a Join as payer rep button under browser-callee transport"
```

---

### Task 8: End-to-end manual verification

**Files:** none — this task changes no code. It is the only step that proves the feature works, because none of the tests above exercise a real LiveKit room, real STT/TTS, or a real agent.

**Interfaces:**
- Consumes: everything from Tasks 1–7.

- [ ] **Step 1: Boot the stack with the flag on**

```bash
cd vera-backend
just up && just migrate
VERA_BROWSER_CALLEE_TRANSPORT=true just api
```

In a second terminal:

```bash
cd vera-backend && VERA_BROWSER_CALLEE_TRANSPORT=true just worker
```

In a third:

```bash
cd vera-frontend && VITE_BROWSER_CALLEE_TRANSPORT=true npm run dev
```

- [ ] **Step 2: Confirm the API booted clean**

Watch the `just api` log for two loop windows (~60s) with no Redis timeout tracebacks and no `dispatch pass failed` lines. A background-loop change is not verified by pytest alone.

- [ ] **Step 3: Enqueue a form with no SIP trunk configured**

In the UI, take a patient form with a valid E.164 payer phone number and send it to the queue. Confirm the enqueue is accepted (before this change it would 409 with "outbound calling is not configured for this tenant").

- [ ] **Step 4: Confirm dispatch without a dial**

In the `just api` log, expect `dispatch: initiated call <id> for form <id> (mode=full)` and **no** `outbound dial failed`. The call appears in Live Monitoring within a couple of seconds, status Initiated.

- [ ] **Step 5: Join within 60 seconds**

Open the call, click **Join as payer rep**, and allow microphone access. Expected: the agent greets you within a few seconds. If nothing is spoken, check the worker log for `wait_for_speaker: outcome … = CallFailed` (you missed the 60s window — requeue and try again) or for a takeover message (a `vera.mode` attribute leaked onto the callee token — a Task 4 regression).

- [ ] **Step 6: Confirm the call went ACTIVE**

The Live Monitoring duration timer starts counting, which only happens on the SSE `active` event. Verify in the DB:

```bash
psql "$VERA_DATABASE_URL" -c "select current_status from call order by created_at desc limit 1;"
```

Expected: `active` — not `initiated`. If it reads `initiated`, Task 5's `should_emit_answered` is not firing.

- [ ] **Step 7: Hold a conversation and confirm answers land**

Answer the agent's questions as a payer rep would. Confirm transcript turns render live, and that answers appear in the form panel. Then:

```bash
psql "$VERA_DATABASE_URL" -c "select count(*) from field_answer where source = 'ai_call';"
```

Expected: a non-zero count that grows as you answer. Zero answers with a healthy-sounding call means the Observer never ran — check the worker log for Redis errors.

- [ ] **Step 8: Confirm teardown**

Close the modal. The room is deleted, the worker session shuts down, and the form transitions to `AI_PROCESSING`. Confirm the call reaches a terminal status and no room is left behind:

```bash
psql "$VERA_DATABASE_URL" -c "select current_status from call order by created_at desc limit 1;"
```

- [ ] **Step 9: Confirm the flag is genuinely off by default**

Restart `just api` **without** `VERA_BROWSER_CALLEE_TRANSPORT`. Enqueueing the same form must 409 with "outbound calling is not configured", and `GET /calls/{id}/join-token?callee=true` must 409. This is the safety property — verify it, do not assume it.

- [ ] **Step 10: Run `/simplify`, then both gates**

Per the repo workflow rule, run the `/simplify` skill on the whole change, then:

```bash
cd vera-backend && just check
cd ../vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build
```

Expected: both gates green on the exact tree you will push.

- [ ] **Step 11: Commit any simplifier refinements**

```bash
git add -A
git commit -m "refactor: simplify browser-callee transport per code-simplifier"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| 1. The switch (setting + gateway property + Vite var) | 1, 7 |
| 2. Enqueue gate skips trunk, keeps E.164 | 2 |
| 3. Dispatcher skips trunk lookup + dial | 3 |
| 4. `callee=true` join token, no `vera.mode`, no lock, 409 when off, `CALL_CALLEE_JOIN` | 4 |
| 5. Worker fires `answered` for a browser callee | 5 |
| 6. Frontend button reusing `LiveCallRoom` | 6, 7 |
| Testing section (unit, integration, frontend, manual) | 1–8 |

All ten files from the spec's file table are covered. The spec's "Risks → flag drift" mitigation (a clear 409, not a broken call) is implemented in Task 4 Step 4 and surfaced by Task 7's `handleJoinFailed`.

**Type consistency:** `browser_callee_transport` (Python property, Tasks 1–4) and `browser_callee` (the local in Task 3, the kwarg in Task 2, the metadata key in Tasks 3/5) are deliberately distinct names for distinct things — the gateway property, the per-call-site parameter, and the wire key. `LiveCallMode` is defined once in Task 6 and consumed unchanged in Tasks 6 and 7. `getJoinToken`'s new signature is defined in Task 6 and used in Task 7. `should_emit_answered` is defined and used within Task 5.
