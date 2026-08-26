# The Observer's unchanged-skip discards provenance — design

**Date:** 2026-08-26
**Branch:** `fix/retry-calls`
**Status:** Approved design. Supersedes the decision section of
`2026-08-26-unchanged-skip-discards-provenance-brief.md`; that brief's *evidence* stands.
**Predecessor:** `.superpowers/sdd/2026-08-25-retry-decision-backend/` (spec B1–B7, merged)

## 1. The defect

A retry can only ever verify a field whose value **changed**. When the payer's representative
repeats the previous answer — the strongest confirmation there is — the Observer discards it and
the field keeps its old provenance forever.

`apps/agent_worker/src/agent_worker/observer.py:557`:

```python
if self._on_file.get(answer.field_path) == answer.value:
    if self._recorded.get(answer.field_path) != answer.value:
        self._push_recorded(answer.field_path, answer.value)
    return
```

`_on_file` is seeded from `plan.prefilled` (`observer.py:407`), which `call_plan.py:153` documents
as `{path: raw intake value}` — the skip was designed for intake prefills. But `prefilled` is built
from `current_values_by_path` (`queue_dispatcher.py:459`), which returns **every** current answer
regardless of source. On a retry it therefore carries prior-call values too, and the skip fires far
outside its design intent.

The brief proves the mechanism three ways (row ownership, value comparison, and
`vera.observer.answer_recorded` span presence in ClickHouse) and rules out extraction as the cause.
That evidence is not restated here.

### 1.1 A second, previously unrecorded defect this cures

`policy_number` — the member ID — is `role="confirm", required=True`
(`forms/catalog/ibv_standard.py:401`). `_satisfied` explicitly refuses to count an intake value for
a confirm-role leaf (`review.py:356`): "their declared purpose is payer CONFIRMATION, so the intake
value is the thing to be confirmed, not the confirmation."

So today, when the bot reads back *"I have the member ID as X — can you confirm that is correct?"*
and the rep confirms, the skip fires, no `ai_call` row is written, `source` stays `intake`, and
`_satisfied` returns **False permanently**. The field whose entire purpose is payer confirmation
cannot be satisfied by a successful confirmation; it becomes satisfied only if the rep
*contradicts* it. `spouse_partner_name` and `spouse_partner_dob` share the shape under family
coverage.

This was not in the brief. It is the same skip, and the same fix closes it.

## 2. Design

### 2.1 The change

Swap the dedup key in `_record_locked` from `_on_file` — which conflates prefill provenance with
this call's own writes — to `_recorded`, which is already defined as "What THIS CALL collected"
(`observer.py:413`).

```python
async def _record_locked(self, answer: ExtractedAnswer, evidence_seq: int | None) -> None:
    if self._recorded.get(answer.field_path) == answer.value:
        return
    ...  # write, emit, _on_file[path] = value, _push_recorded(...) — all unchanged
```

The entire skip branch goes, including its `_push_recorded` call. That is safe: after the change
the only remaining `_push_recorded` call site runs after a completed write, so the controller still
always learns the value — which is what the removed branch's comment ("otherwise the field is owed
for the rest of the call") existed to guarantee.

This is the whole production change. No `CallPlan` field, no Redis plan-shape change, no
control-plane work, no `extra="forbid"` deploy ordering.

### 2.2 Why "did this call write it" needs no transport

The rule could equally be stated as "skip only when the on-file value carries the current call's
id." That framing would require the control plane to stamp `{path: call_id}` onto the `CallPlan`.
It is unnecessary: after a write the observer would stamp the current call's id anyway, which is
exactly what pushing to `_recorded` already does. The two rules are behaviourally identical across
every case — prior-call prefill, intake prefill (`call_id IS NULL`), no prefill, repeat, and
revert-to-prefill — so the local structure is preferred.

### 2.3 `_on_file` is untouched

`_on_file` keeps all three of its other jobs, unchanged:

* the rule engine — `self._rule_engine.evaluate(self._on_file)` (`observer.py:620`);
* `_derive_remaining_locked`'s "a rep-stated or prefilled remaining wins" guard
  (`observer.py:669`);
* the canonical everything-on-file map, including the canonicalization applied at seed time.

`_derive_remaining_locked` routes its derived value through `_record_locked`, so it inherits the
new dedup. Its own `_on_file` guard runs first and is unaffected: a non-blank prefilled remaining
still short-circuits before any write.

### 2.4 The change can only add writes, never remove one

`_push_recorded` has exactly two call sites (`observer.py:564`, `observer.py:588`). The first is
the skip branch being deleted; the second runs only after a successful write.

The full argument, tightened during review from a call-site count into a proof: `_on_file` has
exactly ONE mutation site in the file, and it sits immediately beside `_push_recorded` in the
post-write block. So `_recorded[p] = v` and `_on_file[p] = v` are always set together for the same
`(p, v)`, giving `_recorded[p] == v` ⟹ `_on_file[p] == v` throughout a call. The contrapositive is
the property that matters: **whenever the old guard did not skip, the new guard does not skip
either.** Every path that writes today still writes. This is a strict superset, which bounds the
blast radius — no field can start being written *less*.

## 3. Consequences

### 3.1 What is fixed

| | today | after |
| --- | --- | --- |
| retry: rep repeats a prior non-authoritative call's value | skipped, stays Unverified forever | written under the new call's id |
| first call: rep confirms a `confirm`-role intake prefill (`policy_number`) | permanently unsatisfied (§1.1) | satisfied once judged |
| first call: rep confirms an `ask`-role intake prefill | never counted by `verified_pct` | counted once judged |
| `collected_per="call"` paths (`is_insurance_active`) | dropped when the value repeats | written per call |

The last row resolves the brief's open question 6 and review §15.3: it is the same skip, so the
same fix covers it.

### 3.2 Row volume is bounded, and unchanged from today's bound

Today the write branch ends with `self._on_file[path] = value`, so a second identical extraction in
the same call re-enters the skip. Keying on `_recorded` reproduces that bound exactly: **at most
one row per (path, distinct value) per call**, which is what the existing
`test_unchanged_value_is_recorded_once` already pins.

The worst case is a form that never captures a reference number across its full retry budget: every
repeated value is rewritten on each attempt, so ~170 paths × 5 attempts. Bounded and acceptable.
Nothing downstream assumes one row per `(form, path, call)`; the `fa_current_uq` partial-unique
index constrains *current* rows only, and `record_answer`'s demote-then-flush-then-insert maintains
it through the swap.

### 3.3 No dispute opens on current data; one legacy case must be pinned

`dispute_view` returns `None` when `normalize_value(current) == normalize_value(baseline)`
(`review.py:105`), and `baseline_value` filters on `source` and deliberately **not** on
`is_current` (`field_answers.py:64`) — "so it still resolves after an `ai_call` answer supersedes
it." Writing an unchanged value therefore leaves the dispute verdict identical. This closes the
brief's open question 3.

**One narrow exception, which gets its own test.** Both writers canonicalize today — intake at
`api/v1/patient_forms.py:262`, the extractor at `observer.py:141` — so new data is safe. But
`_on_file`'s own comment records that "a prefill written before the writers canonicalized carries
whatever spelling its source used." For such a legacy row the fix writes the canonical spelling
against a non-canonical baseline, and `normalize_value` only strips and lowercases. A difference
that `canonical_answer` folds but `normalize_value` does not — money format (`$0.00` vs `$0`), or a
hyphen (`Self-Insured` vs `Self Insured`) — would open a spurious dispute on first re-record, and a
spurious dispute blocks form completion.

### 3.4 The second dedup layer does not block the write

`record_answer`'s replay guard is `current.source == source and current.call_id == call_id`
(`field_answers.py:111`). For a new call `current.call_id` is the prior call's id, so the guard
falls through to demote-then-insert. For an intake row `current.source` is `intake` against a
`source` of `ai_call`, so it falls through as well. This closes the brief's open question 4 —
and it is proven by test, not by inspection (§5.2).

### 3.5 Accepted regression: `ask`-role intake fields become judge-conditional

`is_field_satisfied` is asymmetric (`review.py:263`): `intake`/`human` is unconditionally `True`,
while `ai_call` requires `ai_supported` **and** `ai_confidence >= floor`. Demoting an intake row
therefore moves an `ask`-role field from unconditionally trusted to judge-conditional. It reaches
`unsatisfied_required_paths` (the auto-complete gate) and `retryable_required_paths`
(retry-worthiness) through `_unsatisfied`.

Bounding this precisely:

* **`completion_pct` is unaffected** — `completion_pct_v2` is value-presence only (`review.py:149`).
* **`verified_pct` improves** — it routes through `is_call_confirmed`, which is the thing being
  fixed.
* **No transient window on the normal path.** `evaluate_call` is "extract, persist, judge, and
  update status" in one transaction (`post_call_eval.py:148`), so `ai_supported` is written before
  `unsatisfied` is computed.
* **The real exposure is the fallback path.** `resolve_ai_processing` runs no judge, so
  `ai_supported` stays `NULL` and `load_field_status` maps that to `ai_supported=None`, which
  "already fails the gate" (`field_status.py:69`). That path is reached when
  `post_call_eval_ready` is false (`settings.gcp_project is None`) — a configuration in which
  `verified_pct` is already unreliable.
* **The mechanism is not new.** A *changed* value already flips source today; what changes is the
  population, from "fields whose value changed" to "every confirmed prefill."

Accepted rather than mitigated, and pinned by a test (§5.2) so it is recorded behaviour rather than
a production discovery.

### 3.6 Accepted UX gap: the displayed `source` flips

`build_field_views` returns `source` straight through (`review.py:234`). A field the clinic typed at
intake and the rep confirmed will display as `ai_call`. That is true of the current row, but a
reviewer may read it as "the AI made this up." Accepted for this change; recorded as frontend
follow-up F-a (§7).

## 4. Explicitly out of scope

### 4.1 Realtime clearing of the Unverified pill

The pill is `provenance?.authoritative === false` (`vera-frontend/src/components/ibv/FieldRow.tsx:90`),
and `authoritative` is purely `call_id ∈ authoritative_calls` (`call_provenance.py:100`) — the judge
is a separate `JudgeInfo` field and is **not** in that predicate. So after this change the backend
is correct the moment the row lands and the call has its reference number. The UI still will not
show it, for two independent reasons:

1. the `field_answer` SSE envelope carries `field_path, value, source, confidence, completion_pct,
   dispute, ts` and no provenance at all (`vera-frontend/src/lib/api/callEvents.ts:119`);
2. `LiveCallModal` deliberately does not refetch mid-call — "a refetch would wipe live answers
   applied so far" (`LiveCallModal.tsx:76`) — so the provenance map is the snapshot taken when the
   panel was expanded.

There is also a shape problem: the reference number is captured at wrap-up (in the brief's evidence,
18:29:15 on a call running 18:17–18:29), and at that instant **every** field the call already wrote
becomes authoritative retroactively. That is not expressible as a per-answer field on the answer
event; it needs a "this call became authoritative" signal or a targeted refetch.

Deliberately deferred. Bundling it would make the live gate ambiguous — a failure could not be
attributed to the backend not writing versus the UI not updating.

### 4.2 Everything else

* The two retry gates sharing a number but not a decision (the final review's top follow-up).
* The frontend plan (spec F0–F5, B8).
* `is_field_satisfied`'s intake/`ai_call` asymmetry (§3.5) — a core gate, not this change's business.

## 5. Testing

### 5.1 Existing tests over this branch

`apps/agent_worker/tests/unit/test_observer.py` is the relevant root (`testpaths` includes
`apps/agent_worker/tests`). Three tests sit on this code path:

* **`test_unchanged_value_is_recorded_once` (line 233)** — no prefill, rep repeats, one record.
  **Passes unchanged**, and keeps proving the row-volume bound (§3.2).
* **`test_confirming_an_ask_role_prefill_still_reaches_the_controller` (line 242)** — asserts
  `run_state.records == []` and `bus.events == []`. This test *is* the defect, asserted. The
  controller assertion stays; the two no-write assertions invert.
* **`test_a_prefill_is_snapped_before_it_seeds_the_gate_baseline` (line 257)** — must be
  **re-pointed, not merely updated**. Its stated purpose is that `_on_file` is snapped so the rule
  engine compares byte-exact, but it proves that only through the dedup side-effect
  (`records == []`). Once dedup stops keying on `_on_file`, the assertion can no longer fail if
  snapping breaks — the test goes vacuous. It must instead observe `_on_file` through the rule
  engine (`observer.py:620`), which is the consumer its own docstring names. Assert on the engine's
  directive, so an unsnapped seed fails the comparison the test exists to protect.

That last item is not optional. This branch's history includes eight tests that passed with their
feature deleted; silently letting a fourth rot while editing it is the worst available outcome.

### 5.2 New tests

Every assertion below is mutation-proofed, and the mutation and its observed failure are recorded
in the ledger.

1. **The filed defect.** Prefill from a prior call; rep repeats it; assert a record and an emit.
   *Mutation:* restore the `_on_file` dedup key → red.
2. **The confirm-role cure (§1.1).** Two tests, one per layer, because the cure spans both:
   (a) Observer level — a `role="confirm"` leaf with an intake prefill, rep confirms, assert a
   record and an emit; *mutation:* restore the `_on_file` dedup key → red.
   (b) `review.py` level — `_satisfied` for a confirm-role path returns `False` when the current
   row is `intake` and `True` when it is `ai_call` with a supporting judge verdict; *mutation:*
   delete the confirm-role clause at `review.py:356` → the `intake` case goes green, so red.
   Together they show the write happens *and* that it is the write which changes the verdict.
3. **Reaching the DB past the second dedup layer (§3.4).** At `record_answer` level: a current
   `ai_call` row owned by call 1, same value re-recorded under call 2 → returns `True`, demotes,
   inserts. And the intake variant. *Mutation:* widen the replay guard to ignore `call_id` → red.
4. **No dispute opens (§3.3).** Unchanged value re-recorded → `dispute_view` still `None`.
   *Mutation:* compare raw values instead of `normalize_value` → red.
5. **The legacy non-canonical baseline (§3.3).** A stored baseline whose spelling `canonical_answer`
   folds but `normalize_value` does not → assert the resulting dispute state explicitly, so the
   behaviour is chosen rather than discovered.
6. **The accepted regression (§3.5).** An `ask`-role field whose current row is `ai_call` with
   `ai_supported=None` → `is_field_satisfied` is `False`. Pins the fallback-path consequence.
7. **Row-volume bound with a prefill present.** Prefilled value, rep states it three times → exactly
   one record. *Mutation:* drop the `_recorded` guard → red (three records).

### 5.3 Gates

* `just check` — the post-merge baseline is 2708 passed, **0 failed, 0 errors** (commit `11213bec`).
  The branch no longer carries a residue allowance, so any red is real.
* `mypy --strict`, `ruff check`, `ruff format`.
* **Live gate, required.** `just check` is not sufficient — this changes voice-path behaviour. Take
  a call in which the rep repeats a value written by a prior **non-authoritative** call, and confirm
  the field flips from Unverified to verified. That is the exact scenario the brief's screenshot
  showed. Browser-callee transport: `VERA_BROWSER_CALLEE_TRANSPORT=true` on both `just api` and
  `just worker`, `VITE_BROWSER_CALLEE_TRANSPORT=true` on the frontend; ~60s join window, one tab per
  call.
* **Secondary live observation** (same call, no extra setup): confirm `policy_number` is written
  when the rep confirms the read-back member ID rather than contradicting it (§1.1).

### 5.4 Known traps

* `just seed-retry-form` rewrites tenant config and silently reset a tuned
  `retry_fill_threshold` mid-test. Re-apply tenant settings **after** seeding.
* Test-DB residue in `vera_retry_call_fix_test` previously produced 6 failures + 39 errors that
  looked like code. `TRUNCATE patient_form CASCADE; TRUNCATE auth_audit_log; TRUNCATE app_user
  CASCADE;` clears it, and also drops a `form_schema`, so re-seed afterwards.
* Langfuse `observations.input`/`output` are NULL in ClickHouse — payloads live in MinIO. Use span
  *presence* (`vera.observer.answer_recorded`) as the signal. ClickHouse needs no API key:
  `docker exec vera-backend-langfuse-clickhouse-1 clickhouse-client`.

## 6. Constraints

* **PHI.** Answer values are PHI. The existing span already records path, confidence and task only,
  never the value; keep it that way. Never log a value.
* The agent worker has no `FormSchemaDoc` at runtime — but this design needs none, since it adds no
  schema-derived input.
* `vera_core` must not import from `apps/` — not engaged; the change is worker-local.

## 7. Follow-ups recorded, not fixed

* **F-a** — the displayed `source` flips from `intake` to `ai_call` for a confirmed prefill (§3.6).
  The demoted intake row is still reachable via `baseline_value`, but nothing surfaces it when the
  values agree.
* **F-b** — realtime Unverified clearing (§4.1): provenance on the answer envelope plus a
  became-authoritative signal or targeted refetch.
* **F-c** — the fallback path (`post_call_eval_ready == False`) leaves every `ai_call` row
  permanently unjudged, so §3.5's consequence is permanent there.
