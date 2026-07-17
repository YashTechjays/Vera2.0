# Post-Call Pipeline — End-to-End Flow (PRs #72, #75, #79)

Together, these three PRs teach Vera to **fill the form from the phone call by
itself, call back if it missed something, and show a human exactly where every
answer came from — with an Excel export at the end.**

```
 PR #72                        PR #75                       PR #79
 "Read the call,               "If the form is still        "Show reviewers where every
  fill the form"                incomplete, call back        answer came from + export
                                and ask ONLY what's          the finished form as XLSX"
                                missing"
```

A patient form moves through these statuses:

```
READY → IN_QUEUE → IN_CALL → AI_PROCESSING → ┬→ COMPLETED         (everything answered confidently)
                      ↑                      ├→ IN_QUEUE (retry)  (missing fields → call again)
                      └──────────────────────┘
                                             └→ EXCEPTION_REVIEW  (a human must look)
```

---

## Flow 1 — What happens after a call ends (PR #72)

The moment the phone call finishes, step by step:

```
Agent worker (on the call)                Control plane (backend)
──────────────────────────                ─────────────────────────────────
1. call ends
2. pushes "call.ended" into  ──────────►  3. WorkerEventConsumer._handle_call_ended
   Redis stream                               (control_plane/worker_events.py)
                                          4. close_call() — marks the call COMPLETED
                                             and parks the form in AI_PROCESSING
                                          5. _enqueue_post_call_eval():
                                             • saves a "before" snapshot of the
                                               form's current answers (CallFormSnapshot)
                                             • drops a tiny job {tenant, form, call}
                                               into the "vera:post-call" Redis stream
                                             (no PHI in the job — just IDs)
```

Then a separate background loop picks the job up:

```
6. PostCallConsumer (control_plane/post_call_consumer.py) reads the job
7. build_turns() — grabs the call transcript from Redis
8. evaluate_call() (vera_core/services/post_call_eval.py) — the brain. In order:
   a. Is the form still in AI_PROCESSING?  No → someone else handled it, stop.
      (makes redelivery of the same job harmless)
   b. No transcript? → EXCEPTION_REVIEW ("no_transcript")
   c. Load the form's schema. Unreadable? → EXCEPTION_REVIEW ("unsupported_schema")
   d. EXTRACT: send transcript + field list to Gemini Flash →
      "which fields did the rep answer, and what did they say?"
   e. Write each answer as a FieldAnswer row (an older answer for the same field
      is demoted; duplicate paths from the LLM are deduped — last one wins)
   f. JUDGE: a second Gemini call double-checks each answer against the transcript —
      "is this really supported by what was said?" with a confidence score
   g. Recompute the form's completion % and fill the snapshot's "after" state
   h. DECIDE (this is where PR #75 plugs in — see Flow 2)
9. Job is acknowledged. If anything blew up, the job is retried later —
   at most 5 deliveries (MAX_DELIVERIES in stream_consumer.py), then dropped
   for the pipeline sweeper to route the form to review.
```

**Simple version:** call ends → note is dropped in a queue → a worker re-reads
the transcript with an LLM, fills in the form, has a second LLM double-check
it, then decides what's next.

---

## Flow 2 — The decision + retry call (PR #75)

Step 8h above, in plain words:

```
Are ALL required fields answered confidently?
│   ("confidently" = judge said supported, judge confidence ≥ 70,
│    or the value came from a human/intake — those are trusted)
│
├── YES → form is COMPLETED. Done, no human needed.
│
├── NO, and the missing ones are ASKABLE on a call, and retries remain
│        → form goes back to IN_QUEUE with the retry budget reduced by 1
│
└── NO, but a retry can't help (nothing askable is missing, or budget spent)
         → EXCEPTION_REVIEW with a reason ("retries_exhausted" / "unsatisfied_unaskable")
```

The relevant helpers live in `vera_core/forms/review.py`
(`unsatisfied_required_paths` — the authoritative completeness check over ALL
roles, evaluated against the form's real values; `retryable_required_paths` —
the askable subset) and `vera_core/services/field_status.py`
(`load_field_status` — per-field source / judge-supported / judge-confidence,
PHI-free).

When the retry call is dispatched (`vera_core/services/queue_dispatcher.py`):

```
1. Dispatcher sees the form's mode is RETRY
2. Computes WHICH fields are still missing → turns them into human labels
   ("Copay Amount", "Plan Fund Type" — schema titles only, never patient data)
3. Puts those labels into the call room's metadata as "retry_fields"
4. Writes a CallLineage row: "call #2 is a retry of call #1"
5. The agent worker sees retry_fields and overlays its plan prompt
   (retry_focus_block in agent_worker/prompt.py, riding
   PlanRunController.extra_instructions):
   "RETRY CALL. A previous call already collected most of this verification.
    Collect ONLY these missing data points, then politely close."
6. Call ends → Flow 1 runs again → maybe COMPLETED this time.
```

So the loop is: **call → read → fill → still gaps? → focused call-back → read
again** … until complete, out of retries, or unfixable — then a human.

---

## Flow 3 — Review & export (PR #79, the human side)

User flow in the app:

```
Reviewer opens a form in the worklist
│
├── Sees a "Needs Review" tab with WHY it's there ("retries exhausted", etc. —
│     patient_form.review_reason, stamped/cleared only by FormStateMachine)
│
├── Hovers a field → provenance tooltip:
│     "Filled on call attempt 2 (retry) · judge: supported, confidence 84"
│     (per-field provenance rides the form-detail response)
│
├── Opens the "Call history" tab →
│     GET /patient-forms/{id}/calls → attempt timeline built by
│     vera_core/services/call_provenance.py from CallLineage + snapshot diffs:
│     "Attempt 1 (full call) changed 24 fields · Attempt 2 (retry of 1) changed 3"
│     (field NAMES only — and every view of PHI is audit-logged via
│      emit_phi_read_audit)
│
└── Clicks "Export XLSX" →
      POST /patient-forms/{id}/export  (needs the forms:export permission,
      seeded for TENANT_ADMIN + SUPERVISOR)
      backend builds the workbook in memory (vera_core/forms/export.py:
      Form sheet + Provenance sheet), neutralizes anything formula-shaped
      (a value like "=HYPERLINK(...)" becomes inert text — it can never
      execute in Excel), records a ledger row (export_artifact, sha256 of the
      exact bytes) + a FORM_EXPORTED audit record, and streams the file down
      with no-store headers.
```

---

## The safety nets (why this doesn't get stuck or lie)

- **Job lost or consumer down?** The pipeline sweeper
  (`sweep_stuck_ai_processing` in `control_plane/post_call.py`) finds forms
  stuck in `AI_PROCESSING` and routes them to human review. Nothing holds a
  call slot forever.
- **Job keeps crashing?** After 5 delivery attempts it's dropped (instead of
  re-billing the LLM forever) — the sweeper again catches the form.
- **Same event delivered twice?** Every handler checks the form's current
  status first, so a replay is a harmless no-op.
- **LLM unsure or weird?** Anything the judge doesn't back confidently goes to
  a human, never silently into COMPLETED. The whole system fails *toward*
  human review, never away from it.

**One sentence for the whole stack:** the call ends, an LLM reads the
transcript and fills the form, a second LLM grades every answer, the system
calls back for whatever's still missing, and whatever it can't confidently
finish lands in front of a human — who can see the full history of every field
and export it, with an audit trail at every step.
