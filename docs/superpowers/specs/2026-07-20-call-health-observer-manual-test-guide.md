# Handoff: Manually testing the Call Health Observer (no telephony needed)

Paste this whole document as your prompt in a fresh Claude Code session (any worktree
on `feat/call-health-observer-agent`, or on `dev` after PR #112 merges) to drive the
same manual test this feature was verified with — no real phone call required. The
trick: the agent worker's observer is the *only* piece that needs a live call; every
downstream layer (control plane, DB, SSE, frontend) reacts the same way whether the
`call.health` event came from a real analysis or a hand-crafted Redis `XADD`. Everything
below simulates that one event.

Design reference: `docs/superpowers/specs/2026-07-17-call-health-observer-design.md`.

## 0. What you're testing

A per-call LLM health score that can escalate a call to `critical`, write an
append-only episode log, and push a realtime alert (toast + persistent bell inbox) to
authorized users — all without touching the voice pipeline.

## 1. Prerequisites

From `vera-backend/`:
```bash
just up                      # postgres, redis, livekit, sendria (docker compose)
just migrate                 # applies the call-health migrations
just seed                    # sample tenant + admin login (idempotent)
just test_seed_patient_data  # at least one patient_form to attach calls to
```

Ensure `vera-backend/.env` (or your shell) has:
```
LOCAL_KMS_MASTER_KEY=<32-byte base64>   # python3 -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
VERA_LIVEKIT_URL=ws://localhost:7880
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=secret
```

Two long-running terminals:
```bash
cd vera-backend && just api      # control plane on :8000
cd vera-frontend && npm run dev  # Vite dev server on :5173
```

`just seed`'s defaults (override via `SEED_ADMIN_EMAIL`/`SEED_ADMIN_PASSWORD`/
`SEED_TENANT_SLUG` env vars if you changed them):
- Workspace/tenant slug: `vera-health-example`
- Email: `admin@veratechsolutions.example`
- Password: `dev-password-change-me`

Find your actual Docker container names once (compose project = directory name, so
these are usually stable, but verify):
```bash
docker ps --format '{{.Names}}' | grep -E "postgres|redis"
# expect: vera-backend-postgres-1, vera-backend-redis-1
```
Substitute your own names below if different.

## 2. Look up the ids you'll need

```bash
PG="vera-backend-postgres-1"   # adjust if yours differs
RDS="vera-backend-redis-1"

TENANT=$(docker exec $PG psql -U vera -d vera -tAc \
  "SELECT id FROM tenant WHERE slug='vera-health-example';")
ADMIN=$(docker exec $PG psql -U vera -d vera -tAc \
  "SELECT id FROM app_user WHERE email='admin@veratechsolutions.example';")
FORM=$(docker exec $PG psql -U vera -d vera -tAc \
  "SELECT id FROM patient_form LIMIT 1;")

echo "tenant=$TENANT admin=$ADMIN form=$FORM"
```

## 3. Create a synthetic ACTIVE call

**Constraint to know:** `uq_call_active_form` allows at most one non-terminal call per
`form_id`. If you want two *concurrent* demo calls (to test disambiguation), clone a
second `patient_form` row first:

```bash
FORM2=$(docker exec $PG psql -U vera -d vera -tAc "
INSERT INTO patient_form (id, tenant_id, schema_version_id, status, intake_payload,
  patient_name, completion_pct, retry_count, created_at, updated_at)
SELECT gen_random_uuid(), tenant_id, schema_version_id, status, intake_payload,
  'Demo Patient B', completion_pct, retry_count, now(), now()
FROM patient_form WHERE id='$FORM'
RETURNING id;")
```

Insert the call(s) — `initiated_by_id` must be your admin user so it's owner-visible:

```bash
CALL1=$(docker exec $PG psql -U vera -d vera -tAc "
INSERT INTO call (id, tenant_id, form_id, mode, current_status, rep_info,
  completion_pct, started_at, published, initiated_by_id, created_at, updated_at)
VALUES (gen_random_uuid(), '$TENANT', '$FORM', 'full', 'active', '{}', 0, now(),
  false, '$ADMIN', now(), now())
RETURNING id;")

# optional second concurrent call, using the cloned form
CALL2=$(docker exec $PG psql -U vera -d vera -tAc "
INSERT INTO call (id, tenant_id, form_id, mode, current_status, rep_info,
  completion_pct, started_at, published, initiated_by_id, created_at, updated_at)
VALUES (gen_random_uuid(), '$TENANT', '$FORM2', 'full', 'active', '{}', 0, now(),
  false, '$ADMIN', now(), now())
RETURNING id;")

echo "CALL1=$CALL1  CALL2=$CALL2"
```

## 4. Simulate the observer — inject a `call.health` event

This is the one line that stands in for the agent worker's LLM analysis. It lands on
the same Redis stream (`vera:worker-events`) the real observer publishes to, so the
control-plane consumer processes it identically.

```bash
ROOM1="call--$TENANT--$CALL1"
docker exec $RDS redis-cli XADD vera:worker-events '*' event \
  "{\"type\":\"call.health\",\"room_name\":\"$ROOM1\",\"score\":25,\"flag\":\"conversation_loop\",\"reason\":\"Simulated: agent repeated the same verification question.\",\"turn_count\":8,\"ts\":$(date +%s000)}"
```

Wait ~2s, then verify:
```bash
docker exec $PG psql -U vera -d vera -tAc \
  "SELECT current_status, health_score, health_flag, health_reason FROM call WHERE id='$CALL1';"
# expect: critical | 25 | conversation_loop | Simulated: ...

docker exec $PG psql -U vera -d vera -tAc \
  "SELECT event_type, event_value FROM call_event WHERE call_id='$CALL1' ORDER BY created_at;"
# expect exactly 2 rows: status|critical  and  health|conversation_loop
```

Valid `flag` values: `none`, `conversation_loop`, `repeated_questions`,
`hallucination`, `long_silence`, `off_script`, `low_confidence`,
`supervisor_requested`, `other`. Score is 0–100.

**Recovery requires 2 consecutive healthy events** (anti-flap hysteresis — the first
healthy read only updates the columns, the second closes the episode back to
`active`):
```bash
for i in 1 2; do
  docker exec $RDS redis-cli XADD vera:worker-events '*' event \
    "{\"type\":\"call.health\",\"room_name\":\"$ROOM1\",\"score\":85,\"flag\":\"none\",\"reason\":\"Simulated: back on script.\",\"turn_count\":$((10+i)),\"ts\":$(date +%s000)}"
  sleep 2
done
# after the 2nd: current_status back to 'active', two more call_event rows (health=none, status=active)
```

## 5. (Optional) Simulate the live per-call SSE frame

This drives the *open call modal's* live health badge specifically (separate from the
worker-event path above — it rides the per-call transcript stream directly):

```bash
docker exec $RDS redis-cli XADD "vera:call-events:$ROOM1" '*' \
  type health data '{"score":55,"flag":"low_confidence","reason":"Simulated live frame"}' \
  ts $(date +%s000)
```

## 6. Drive it with playwright-cli

```bash
playwright-cli open --headed http://localhost:5173
playwright-cli snapshot
```
Read the snapshot for the login field refs, then:
```bash
playwright-cli fill <workspace-ref> "vera-health-example"
playwright-cli fill <email-ref> "admin@veratechsolutions.example"
playwright-cli fill <password-ref> "dev-password-change-me"
playwright-cli click <sign-in-ref>
playwright-cli snapshot
```

**Checklist — re-snapshot after each action and confirm:**

| Step | What to look for in the snapshot |
|---|---|
| After injecting the flagged event (§4) and being logged in with the tab open | A sonner **toast**: "Call needs intervention" / "Conversation loop — health 25% · #XXXXXX" |
| Bell icon | Unread badge shows the count |
| Click the bell | Popover lists the alert: `Conversation loop — health 25% · #XXXXXX` |
| Live Monitoring table | Row shows `critical` status, red-toned `25%` health cell |
| Hover the health cell (`playwright-cli hover <ref>`) | Tooltip with the flag name + the `reason` text |
| Click the bell item | Navigates + opens **that exact call's** modal (check the "Call {id}" line matches `$CALL1`), header "Call Health" shows a matching score |
| Reload the page (`playwright-cli reload`) | Modal does NOT reopen (nav state is one-shot); bell badge resets to 0 (inbox is session-memory only — this is by design, not a bug) |
| Mark a call terminal then click its (still-open-session) bell item | Page shows **"That call is no longer active."**, no modal, no crash |
| Two concurrent flagged calls (`$CALL1`/`$CALL2`, different flags) | Bell shows two distinguishable entries, e.g. `#A1B2C3` vs `#D4E5F6` — verifies the non-PHI disambiguation |

Screenshot any state with `playwright-cli screenshot --filename=<name>.png`.

## 7. Cleanup

```bash
docker exec $PG psql -U vera -d vera -c "
DELETE FROM call_event WHERE call_id IN ('$CALL1','$CALL2');
DELETE FROM call WHERE id IN ('$CALL1','$CALL2');
DELETE FROM patient_form WHERE id = '$FORM2';"

docker exec $RDS redis-cli DEL "vera:notify:$TENANT" "vera:call-events:$ROOM1"
```

## Gotchas learned the hard way (this session)

- **`uq_call_active_form`**: only one non-terminal call per `form_id` — clone a form
  row (§3) for each *concurrent* synthetic call, or first mark the old call terminal
  before reusing its form.
- **The bell inbox is session-memory only, by design** — it's not persisted, so a page
  reload empties it (only the *read cursor* survives, in sessionStorage). To see a
  populated bell after a reload, fire a fresh event within the 60s server replay
  window, or just don't reload between injecting and checking.
- **Toasts are real-time-only.** The toast fires only for a connection that's live
  *at the moment the notification is published*. If you inject the event before
  logging in / opening the tab, you'll still see it in the bell (DB + replay window
  cover it) but you won't get the toast pop — that's expected, not a bug.
- **`uvicorn --reload` needs a few seconds** after any backend code edit before
  `/healthz` responds again — don't fire requests immediately after a save.
- **Container names** depend on your `docker compose` project name (normally the
  `vera-backend` directory name) — verify with `docker ps` if the commands above 404.
- A **stale placeholder `.env`** can shadow your real dev secrets if an earlier
  automated run created one — check `vera-backend/.env` if login unexpectedly fails.
