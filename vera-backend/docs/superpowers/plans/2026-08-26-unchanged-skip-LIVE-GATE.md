# Live gate — Observer unchanged-skip provenance fix

**Branch:** `fix/retry-calls` @ `273d0a11` · **DB:** `vera_retry_call_fix` (localhost:5432)
**Spec:** `docs/superpowers/specs/2026-08-26-unchanged-skip-discards-provenance-design.md`

**What you are proving:** the rep repeating a value written by a *prior* call now produces a
`field_answer` row owned by the **current** call, so the field can become verified. Before the fix
that repetition was discarded and the earlier call kept the row forever.

`just check` cannot prove this — every assertion in the suite is against fakes, and the defect was
found in production, not by a test.

---

## 0. Setup (once)

```bash
cd vera-backend
just up                      # postgres, redis, livekit, sendria
just migrate
just seed                    # baseline schemas + prompts + sample tenant
```

Three terminals, and **the env var must be on both backend processes**:

```bash
# terminal 1 — API. VERA_GCP_PROJECT is what turns the post-call JUDGE on.
VERA_BROWSER_CALLEE_TRANSPORT=true VERA_GCP_PROJECT=<your-gcp-project> just api

# terminal 2 — worker
VERA_BROWSER_CALLEE_TRANSPORT=true just worker

# terminal 3 (frontend)
cd vera-frontend && VITE_BROWSER_CALLEE_TRANSPORT=true npm run dev
```

**`VERA_GCP_PROJECT` is not optional if you want `verified_pct` to move.** `main.py:237` decides
`post_call_eval_ready = settings.gcp_project is not None`. Unset, the call closes through the
FALLBACK path (`resolve_ai_processing`, audit `trigger=call.ended`), no judge runs, every row keeps
`ai_supported = NULL`, `is_call_confirmed` returns False regardless of authority — so `verified_pct`
stays NULL and Scenario B3 has no `field_evaluation` rows to inspect. The row-ownership assertion
and the Unverified pill still work without it; nothing else does.

Confirm the judge actually ran afterwards: the audit trail should show `trigger=post_call_eval`,
not `trigger=call.ended`.

**Vertex trap:** `VERA_VERTEX_LOCATION` defaults to `us-central1`, which 404s for Gemini in this
project. If the judge errors, set `VERA_VERTEX_LOCATION=global`. Needs working ADC
(`gcloud auth application-default login`).

Browser-callee places **no SIP call** — no real payer is ever dialled. You join the LiveKit room
from Live Monitoring and play the payer's representative yourself.

**Two constraints that bite:** the join window is **~60 seconds** from dispatch, and it is **one tab
per call**. Have the Live Monitoring tab already open before you queue anything.

### The DB handle you will use throughout

```bash
psql postgresql://localhost:5432/vera_retry_call_fix
```

RLS: the `vera` role is superuser locally, so queries return rows without setting `app.tenant_id`.
On the dev Cloud SQL instance you would need `SET app.tenant_id = '<uuid>';` first.

---

## Scenario A — THE GATE. Two calls, the first with no reference number.

This is the exact shape of the defect, reproduced from the brief's evidence (form
`01a03a0e-…`: call 1 captured no reference → non-authoritative; call 2 repeated its values and every
repeated field stayed `Unverified`).

**Do not use `just seed-retry-form` for this scenario.** That script seeds a prior call that
*captured* the reference number, which produces a FOCUSED retry — the wrong path. Scenario A needs a
prior call with **no** reference so the second call runs FULL from the top. Use a fresh form.

### A1. Create a form and queue it

Create a patient form through the UI (or `just test_seed_patient_data`), then queue it. Record its
id:

```sql
SELECT id, status, retry_count, completion_pct, verified_pct
FROM patient_form ORDER BY created_at DESC LIMIT 1;
```

### A2. Call 1 — answer some fields, then REFUSE the reference number

Join from Live Monitoring within the 60s window. As the rep:

| the bot asks | say | why |
|---|---|---|
| plan / coverage type | **"Individual"** | an ordinary `ask` field you will repeat verbatim in call 2 |
| funding type | **"Self Insured"** | second repeatable field |
| benefit year | **"Calendar year"** | third |
| member ID read-back (`policy_number`) | **"Yes, that's correct"** | `confirm`-role — the §1.1 case |
| **call reference number** | **"I'm not able to give you a reference number for this call."** | ← **the critical turn.** Without it, call 1 is non-authoritative |

Then let the call end (or hang up).

**Verify call 1 is non-authoritative before continuing — if it captured a reference, the scenario is
void and you must redo it:**

```sql
SELECT call_id, value FROM field_answer
WHERE form_id = '<FORM_ID>'
  AND field_path = 'sections.insurance_representative.call_reference_number';
-- MUST return zero rows (or only rows with call_id IS NULL).
```

Note call 1's id and what it wrote:

```sql
SELECT c.id AS call_id, c.mode, c.created_at
FROM call c WHERE c.form_id = '<FORM_ID>' ORDER BY c.created_at;

SELECT field_path, value->>'value' AS val, source, call_id, is_current
FROM field_answer WHERE form_id = '<FORM_ID>' AND is_current
ORDER BY field_path;
```

### A3. Call 2 — repeat the SAME answers verbatim, and DO give a reference number

Queue the form again and join. As the rep:

| the bot asks | say |
|---|---|
| plan / coverage type | **"Individual"** — *byte-identical to call 1* |
| funding type | **"Self Insured"** |
| benefit year | **"Calendar year"** |
| member ID read-back | **"Yes, that's correct"** |
| **call reference number** | **"Reference number is ABC12345."** ← give it this time |

Repeating the values **verbatim** is the whole point. A paraphrase changes the extracted value, which
would have been written even before the fix and proves nothing.

### A4. THE ASSERTION

```sql
-- Row ownership must have MOVED to call 2 for every repeated field.
SELECT fa.field_path,
       fa.value->>'value'  AS val,
       fa.call_id,
       fa.is_current,
       c.created_at        AS owning_call_started
FROM field_answer fa JOIN call c ON c.id = fa.call_id
WHERE fa.form_id = '<FORM_ID>' AND fa.is_current
  AND fa.field_path IN (
    'sections.benefit_coverage.coverage_type',
    'sections.benefit_coverage.plan_fund_type',
    'sections.benefit_coverage.benefit_year_type',
    'sections.insurance_information.policy_number')
ORDER BY fa.field_path;
```

| | PASS | FAIL (the bug is still live) |
|---|---|---|
| `call_id` on each repeated field | **call 2** | call 1 |
| superseded rows | call 1's rows exist with `is_current = false` | call 1's rows still `is_current = true` |
| form detail UI | no **Unverified** pill on those fields | pill still shown |
| `verified_pct` | risen | flat |

```sql
-- verified_pct is nullable: NULL means "never evaluated", which is NOT the same as 0%.
SELECT completion_pct, verified_pct, status FROM patient_form WHERE id = '<FORM_ID>';
```

### A5. Cross-check in Langfuse (the direct inverse of the original proof)

The defect was proven by `vera.observer.answer_recorded` spans being **absent** for the repeated
paths. Their **presence** now is the inverse. `observations.input`/`output` are NULL in ClickHouse —
payloads live in MinIO — so use span **presence**, never content:

```bash
docker exec -it vera-backend-langfuse-clickhouse-1 clickhouse-client --query "
SELECT count() FROM observations
WHERE name = 'vera.observer.answer_recorded'
  AND start_time > now() - INTERVAL 30 MINUTE"
```

Expect roughly one span per written answer for call 2, including the repeated paths.

---

## Scenario B — the two side effects the final review discovered

These are **live-call effects** that spec §2.3 originally missed: the deleted early return also
short-circuited `_derive_remaining_locked` **and** `RuleEngine.evaluate`. A confirmation-only pass now
reaches both. They are plausibly cures, but they were unrecorded, and this is the run that observes
them. Scenario B uses the seeded focused-retry form, where inherited prior-call state is richest.

```bash
just seed-retry-form
# ⚠️ RE-APPLY TENANT SETTINGS NOW — this script REWRITES tenant config and silently reset a
#    tuned retry_fill_threshold mid-test on a previous run.
just arm-retry-form
```

Join and, wherever the bot reads a value back, **confirm it unchanged** rather than correcting it.
In particular confirm any deductible **total** it already holds.

### B1. Derived value from a confirmation

```sql
SELECT field_path, value->>'value' AS val, call_id, confidence, is_current
FROM field_answer
WHERE form_id = '<FORM_ID>' AND field_path LIKE '%remaining%'
ORDER BY created_at DESC;
```

**Watch for:** a `remaining` row written by *this* call, derived as total − met, where the rep only
confirmed the **total** and never stated `met` on this call. That is new behaviour. It is not
necessarily wrong — arguably it is a cure — but it must be *seen*, not assumed.

### B2. Rule engine firing on a confirmation-only pass

**Watch the call itself for:** an unexpected early termination, an unexpected task skip, or a
re-ask of a numeric triplet that the rep never contradicted. Pre-fix the rule engine never ran on a
confirmation-only pass; now it does, against inherited prior-call and intake state.

```sql
SELECT event_type, event_value, created_at
FROM call_event WHERE call_id = '<CALL_ID>' ORDER BY created_at;

-- audit_log keys the subject on resource_id (text), with the rest in the `detail` JSONB.
SELECT event_type, detail->>'trigger' AS trigger, detail->>'reason' AS reason, created_at
FROM audit_log WHERE resource_id = '<FORM_ID>' ORDER BY created_at DESC LIMIT 10;
```

### B3. Judge verdicts on the confirmed prefills (spec §3.5)

This is the failure mode that could make the fix *cause* a redial. A terse "yes, that's correct" is
weak textual support, and the judge is asked whether the transcript **supports** the value. If it
answers no, a field that was unconditionally trusted as `intake` becomes unsatisfied.

```sql
-- NOTE: the FK is fe.answer_id (not field_answer_id), and fe.evidence is PHI-tagged —
-- it is transcript text, so it is deliberately NOT selected here.
SELECT fa.field_path, fe.supported, fe.confidence
FROM field_evaluation fe JOIN field_answer fa ON fa.id = fe.answer_id
WHERE fa.form_id = '<FORM_ID>' AND fa.source = 'ai_call'
ORDER BY fe.supported, fe.confidence;
```

**Watch for:** `supported = false` or `confidence` below the review floor on fields the rep merely
confirmed. A handful is expected and acceptable; a systematic pattern means the confirmation-only
evidence is too thin for the judge and §3.5's accepted trade-off needs revisiting.

---

## Scenario C — the negative control (5 minutes, do not skip)

Proves the fix did not turn dedup off wholesale.

Take any call and state **the same value twice in one call** ("Individual" … then "Yes, individual").

```sql
SELECT field_path, count(*) AS rows_this_call
FROM field_answer
WHERE call_id = '<CALL_ID>' AND field_path = 'sections.benefit_coverage.coverage_type'
GROUP BY field_path;
```

**PASS: exactly 1.** More than one means the within-call dedup regressed and every re-extraction is
writing a row — the row-volume bound is gone.

---

## Traps

- **`just seed-retry-form` rewrites tenant config.** Re-apply tenant settings *after* seeding, and
  re-check them immediately before the call. It silently reset a tuned `retry_fill_threshold`
  mid-test on a previous run and the test stopped discriminating without failing.
- **Scenario A is void if call 1 captures a reference number.** Check A2's query before proceeding;
  a focused retry is a different code path and does not test the defect.
- **Paraphrasing in call 2 invalidates the gate.** The value must be byte-identical after
  canonicalisation, or it would have been written pre-fix too.
- **The Unverified pill will NOT clear during the call.** Expected and deferred (spec §4.1): the
  `field_answer` SSE envelope carries no provenance and `LiveCallModal` does not refetch mid-call.
  Reload the form detail after the call. This is not a failure of the fix.
- **Test-DB residue** in `vera_retry_call_fix_test` has produced 6 failures + 39 errors that looked
  like code. Clear with `TRUNCATE patient_form CASCADE; TRUNCATE auth_audit_log; TRUNCATE app_user
  CASCADE;` — note it also drops a `form_schema`, so re-seed afterwards.
- **Langfuse left running inflates `just check` ~180s → ~620s** and can OOM ClickHouse. `just
  langfuse-down` tears down the WHOLE compose project including postgres — recover with `just up`.

---

## Record the result

Append to `.superpowers/sdd/2026-08-25-retry-decision-backend/progress.md`: both call ids, the form
id, the Langfuse trace id, the per-field before/after row ownership from A4, the `verified_pct`
movement, and anything Scenario B surfaced. Note explicitly that the pill did not clear *during* the
call — expected, deferred, not a failure.
