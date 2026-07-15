# Implementation Plan — Dispatch Concurrency-Cap Race + Distributed Twilio CPS Pacing

**Date:** 2026-07-13
**Status:** Proposed (not yet implemented)
**Area:** call queue / dispatcher (`packages/vera_core/src/vera_core/services/queue_dispatcher.py`)
**Related:** `docs/superpowers/specs/2026-06-29-call-queue-dispatch-design.md`, `adr/devops-todo.md`

---

## 1. Context & problem statement

Dispatch today is a direct in-process `await try_dispatch(...)` fired from two FastAPI
handlers right after commit — `api/v1/patient_forms.py:889` (form enqueued) and
`api/v1/calls.py:652` (a call ended, a slot freed). There is **no `LISTEN/NOTIFY`** and
**no background poller**; dispatch is purely edge-triggered.

Two independent defects:

### Problem A — concurrency-cap race (correctness)
`try_dispatch` enforces the per-tenant concurrency cap (`Tenant.max_agents_per_va`) with an
**unlocked** count under READ COMMITTED:

```python
# queue_dispatcher.py ~L106 (step "2. Count active calls")
active_count = SELECT count(*) FROM patient_form
               WHERE tenant_id = :t AND status IN _ACTIVE_FORM_STATUSES   # IN_CALL, AI_PROCESSING
slots = tenant.max_agents_per_va - active_count
# then: candidates fetched FOR UPDATE SKIP LOCKED (L139)
```

`FOR UPDATE SKIP LOCKED` prevents the **same** queued row being dispatched twice, but it does
**not** serialize the count. Two concurrent `try_dispatch` calls (two async requests on one
replica, or two replicas) each read the same stale `active_count`, each compute `slots`, each
lock **disjoint** candidates, and both dispatch → **`active` overruns `max_agents_per_va`**
(`N + 2·(C−N) = 2C−N`). No advisory lock / SERIALIZABLE / isolation override exists anywhere
in the path.

### Problem B — CPS pacing is in-process + inside a long transaction (scalability/correctness)
The dispatcher already knows about the carrier calls-per-second (CPS) limit and paces dials
in-loop (`queue_dispatcher.py` ~L320–330):

```python
# "Pace every dial attempt ~1/s (carrier CPS limit) ... sleep between attempts"
if dial_attempted:
    await asyncio.sleep(dial_pacing_s)      # dial_pacing_s: float = 1.0
dial_attempted = True
await livekit.create_sip_participant(...)   # the actual initiation
```

Two flaws:
1. **Not distributed.** `asyncio.sleep` paces *one process*. With 2 replicas each pacing at
   1/s, aggregate initiation is **2/s** → exceeds the trunk CPS → Twilio rejects with
   **Error 32001 "Trunk CPS limit exceeded"** (SIP trunking *rejects*, it does not queue).
2. **Holds a long transaction.** The paced loop runs inside the caller's request transaction
   (candidates were `FOR UPDATE`-locked; `Call` rows flushed in a `begin_nested()` savepoint).
   A morning burst that dials `slots` forms sleeps `slots × dial_pacing_s` seconds **with row
   locks + a DB connection held open, inside the HTTP request**. At cold start (active=0,
   cap=125, 125 queued) that is ~125 s of one held transaction/request.

Twilio confirmed limits (see conversation / `adr/devops-todo.md`): **default 1 CPS per trunk
per region**, self-serve to ~5 (console) and higher via Twilio; exceeding → **32001 reject**.
Target workload: **1500 calls/day over 8h, ~40-min avg** ⇒ steady-state ≈ **0.05 calls/s**
(~1 every 19 s) and ≈ **125 concurrent** (Little's Law). Steady state is trivial; the risk is
the **morning ramp / retry waves** hammering the trunk faster than CPS.

---

## 2. Goals / non-goals

**Goals**
- G1: `active` calls never exceed `max_agents_per_va`, under any concurrency, single- or
  multi-replica.
- G2: outbound call **initiation** never exceeds the trunk's CPS, coordinated **across
  replicas** — a burst of N queued forms drains at ≤ CPS (e.g. 100 forms → ~1/s → ~100 s).
- G3: no long-lived request/transaction held across a paced burst.
- G4: retries/expiry progress over time (needs a driver, since dispatch is edge-triggered today).

**Non-goals (explicitly deferred)**
- Full outbox + tenant-partitioned Streams architecture (the long-term design; tracked
  separately). This plan is the **correct, sufficient** step for the stated scale, not the
  end-state.
- Auto-reading Twilio's configured CPS (no Twilio creds in-repo) — CPS stays operator-set config.

---

## 3. Phase 1 — Advisory lock (fixes Problem A)

Serialize the **count-and-claim** per tenant with a Postgres transaction-scoped advisory lock.

### 3.1 Change
`packages/vera_core/src/vera_core/services/queue_dispatcher.py`, at the very top of
`try_dispatch` **before** the `active_count` query (step 2):

```python
# Serialize dispatch per tenant so the concurrency-cap count-and-claim is atomic across
# concurrent callers and replicas. xact-scoped → auto-released on commit/rollback.
await session.execute(
    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
    {"key": f"queue_dispatch:{tenant_id}"},
)
```

- Per-tenant key ⇒ different tenants never block each other.
- Lock lives in Postgres ⇒ works across replicas.
- Precedent in-repo: `models/auth.py:184` uses an advisory lock for the audit-chain seq.

### 3.2 Lock-scope decision
- **Recommended end state:** the lock covers **count + candidate claim only**, not the paced
  dial loop. That requires Phase 2's restructure (claim → commit → dial), so the lock is held
  for milliseconds, never across `create_sip_participant`.
- **If Phase 1 ships before Phase 2:** wrapping the whole `try_dispatch` is acceptable at the
  stated scale (~0.05 dispatch/s) **except** it would hold the lock across the existing
  in-loop `asyncio.sleep`. Mitigate by landing Phase 2's de-sleep close behind, or scope the
  lock now by moving the claim ahead of the dial loop. **Do not ship the whole-txn lock while
  the in-loop `dial_pacing_s` sleep still holds the transaction for a full burst.**

### 3.3 Tests (TDD — failing first)
- New integration test under `tests/integration/control_plane/` (must live under CI
  `testpaths`): two concurrent DB sessions run `try_dispatch` for one tenant with a low
  `max_agents_per_va`; assert combined `active` ≤ cap. **Fails today (overrun); passes with the lock.**
- Regression: existing no-double-dispatch (`SKIP LOCKED`) behavior still holds.

### 3.4 Verification
`just check` (ruff + mypy + pytest) → `/simplify` on the change → re-run `just check`.

**Effort:** ~0.5 day. Independent, low-risk, ship first.

---

## 4. Phase 2 — Distributed CPS pacer + ticker (fixes Problem B, and G3/G4)

Replace the in-process `asyncio.sleep(dial_pacing_s)` with a **Redis token-bucket rate limiter
keyed per trunk**, dial only what tokens allow per pass, and add a **ticker** that re-drives
dispatch so bursts drain at CPS over time.

### 4.1 Component — distributed token bucket (Redis)
New module: `packages/vera_core/src/vera_core/services/cps_limiter.py`.
- **Key:** `cps:{trunk_id}` — CPS is a per-trunk carrier limit; keying on `trunk_id` is correct
  for both per-tenant trunks and a shared trunk. (`trunk_id` is already resolved in
  `try_dispatch` via `get_integration_credentials(..., "livekit_outbound_trunk_id")`.)
- **Atomic Lua script** (single round-trip): state = `{tokens, last_refill_ms}`;
  `capacity = CPS`, `refill = CPS/sec`. `try_acquire()` → `True` if a token was consumed.
- Reuse the existing async Redis client (`app.state.redis`, `main.py:105`,
  `redis.asyncio.Redis`) — pass it (or a `CpsLimiter`) into `try_dispatch` the same way
  `livekit`/`kms` are injected. Keep the BLOCK/timeout discipline noted in the repo CLAUDE.md
  if any blocking reads are added (not needed for a bucket).

### 4.2 Enforce at the initiation point, drop the in-txn sleep
In the `for form in candidates` loop (`queue_dispatcher.py`), replace L320–330:

```python
# BEFORE: if dial_attempted: await asyncio.sleep(dial_pacing_s)
# AFTER:
if not await cps_limiter.try_acquire(trunk_id):
    break          # no token this pass → leave remaining forms IN_QUEUE for the ticker
await livekit.create_sip_participant(...)
```

- Per pass you initiate only as many calls as there are tokens (≈1 at CPS=1); the rest stay
  `IN_QUEUE`. No `asyncio.sleep`, so the transaction stays short (fixes G3).
- Keep the existing savepoint + FAILED/retry accounting for a *failed* dial unchanged.
- Remove `dial_pacing_s` (or keep as a no-op-deprecated arg for one release).

### 4.3 Component — ticker (the driver; fixes G4)
New lifespan background task in `apps/control_plane/src/control_plane/main.py` (alongside the
recording jobs wired at ~L195):
- Every `VERA_DISPATCH_TICK_SECONDS` (~1 s), trigger `try_dispatch` for tenants that have
  `IN_QUEUE` / retry-due / expired forms.
- Start as a **single-instance / leader-elected** loop (simplest correct); tenant-partition later.
- Each tick runs a **short** transaction (claim under the advisory lock, dial ≤ tokens, commit).
  Over successive ticks the bucket refills → burst drains at CPS.
- ⚠️ **Repo rule (vera-backend/CLAUDE.md):** a new long-lived background loop MUST be
  **boot-verified** (`just up` → `just api`, watch it idle a couple of windows), not just
  pytest'd. Also mirror the Redis idle/timeout handling conventions if it blocks.

### 4.4 Config — `packages/vera_core/src/vera_core/config/settings.py`
- `dispatch_trunk_cps: int = 1`  (`VERA_DISPATCH_TRUNK_CPS`) — default = Twilio default; must
  match the trunk's console CPS. Add a row to `adr/devops-todo.md` (operator-set, can't be
  auto-read without Twilio creds).
- `dispatch_tick_seconds: float = 1.0` (`VERA_DISPATCH_TICK_SECONDS`).
- (Later) per-tenant/per-trunk CPS override stored with the integration config — deferred.

### 4.5 Burst walkthrough (100 queued, CPS = 1)
```
tick t=0s : advisory-lock → count → slots free, 100 IN_QUEUE → try_acquire→1 token → dial 1, 99 queued → commit
tick t=1s : bucket refills 1 → dial 1, 98 queued → commit
 ...        → ~1 dial/s → 100 calls over ~100 s. Never exceeds trunk CPS → zero 32001 rejects.
```
Retries re-queue and flow through the same bucket → **no retry storm**.

### 4.6 Tests
- **Unit:** token bucket with an injected clock — N `try_acquire` in a 1 s window returns
  exactly CPS `True`s (fakeredis or a fake).
- **Integration:** seed 100 `IN_QUEUE` forms; run dispatch/ticker with a fake LiveKit gateway +
  fake clock; assert ≤ CPS initiations per simulated second and all eventually dispatched;
  assert transaction/lock hold per pass is short (no long sleep).
- **Multi-replica sim:** two limiter instances sharing one Redis assert combined ≤ CPS.
- **Boot test:** ticker idles cleanly.

### 4.7 Verification
`just check` → `/simplify` → re-`just check` → **boot-verify the ticker loop**.

**Effort:** ~2–3 days (limiter + wiring + ticker + config + tests + boot-verify).

---

## 5. Sequencing & dependencies
1. **Phase 1** — advisory lock (correctness; ship independently). If shipped alone, scope it to
   count+claim, *not* around the current in-loop sleep.
2. **Phase 2b** — swap the in-loop `asyncio.sleep` for the Redis limiter `try_acquire` +
   short transactions (removes the long-txn hold; makes the whole-txn lock cheap).
3. **Phase 2a/2c** — the limiter module and the ticker (drives the drain; adds retries/expiry).
4. Config + `devops-todo` row + boot-verify.

Phases 1 and 2b are tightly coupled (lock-hold length depends on 2b); land them together or
2b-first if a large morning burst is imminent.

## 6. How the pieces compose (sanity check)
- **Advisory lock** → never exceed `max_agents_per_va` (~125 concurrent). *(concurrency)*
- **CPS limiter** → never exceed trunk CPS (~1/s) when *starting* calls, across replicas. *(rate)*
- **Ticker** → queued / retry / expired forms progress over time.
- Together: 100-form morning burst → paced at ≤1/s, capped at 125 concurrent, no overrun, no
  32001 storm, no multi-minute held request.

## 7. Open decisions (confirm before build)
1. **Default CPS:** `VERA_DISPATCH_TRUNK_CPS=1` (matches Twilio default) or a higher self-served value?
2. **Trunk topology:** one shared trunk (`ST_BmdFy84WqTpG`) today, or a trunk per tenant?
   (Decides whether the limiter key is effectively global or per-tenant — the code already
   keys on `trunk_id`, so both work; confirms expected cardinality.)
3. **Ticker ownership:** single-instance / leader-elected to start, tenant-partitioned later — OK?
4. **Lock scope:** land the count+claim restructure with Phase 1, or accept a whole-txn lock
   briefly until Phase 2b?

## 8. Out of scope (future)
Transactional outbox + tenant-partitioned Redis-Streams dispatcher with the concurrency cap
kept atomic in Postgres — the long-term scalable design for >10k calls/day or many high-volume
tenants. Not required at the stated scale; this plan is the correct interim step and is
forward-compatible (the advisory-lock cap-claim and the Redis CPS limiter both carry forward).
