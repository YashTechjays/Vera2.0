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

### 2.3 `_on_file`'s contents are untouched — its invocation is not

`_on_file` keeps all three of its other jobs, unchanged in what they read:

* the rule engine — `self._rule_engine.evaluate(self._on_file)` (`observer.py:616`);
* `_derive_remaining_locked`'s "a rep-stated or prefilled remaining wins" guard
  (`observer.py:667`);
* the canonical everything-on-file map, including the canonicalization applied at seed time.

That is true of the map's CONTENTS. It is false of the map's INVOCATION. The deleted early return
used to skip both `_derive_remaining_locked` and `RuleEngine.evaluate` entirely whenever the
extracted value matched what was already on file — the `return` sat before either was reached
(§2.1). Now that dedup keys on `_recorded`, which starts empty for THIS call, a confirmation-only
pass reaches both.

Concretely: prefilled `total` and `met`, a blank `remaining`, and the rep merely confirms the
`total`. Pre-fix, `self._on_file.get('total') == answer.value` was true, so the branch returned
before the write — `records == []`. Post-fix, `_recorded` has nothing for `total` yet this call, so
the write proceeds, `_rule_engine.evaluate` runs, and `_derive_remaining_locked` fires: `remaining`
is blank in `_on_file`, so its own guard does not short-circuit, and it derives `total - met` and
records it under THIS call's id — `records == [total, remaining]`. `remaining` is a DERIVED value,
written against this call, computed from a `met` the rep never stated on this call; it came from
`plan.prefilled`, i.e. from `_on_file`'s inherited prior-call/intake contents.

The rule engine has the same shape (`observer.py:616`): a fire-once `terminate_call` /
`skip_to_task` flow rule, a `Contradiction`, or a `NumericConsistency` `ReAsk` whose `when` rests
purely on inherited prior-call/intake state can now fire on a focused retry where pre-fix the
engine never ran for that pass. `focus_call_plan` clears `on_file_values` but deliberately leaves
`prefilled` untouched (`call_plan.py:482`), so the full prior-call/intake state is present in
`_on_file` on every focused retry — nothing about narrowing to a focused retry narrows what the
rule engine sees.

`_derive_remaining_locked` routes its derived value through `_record_locked`, so it inherits the
new dedup, and its own `_on_file` guard is unaffected in what it checks — a non-blank prefilled
remaining still short-circuits before any write. What changed is that the guard is now REACHED on
a confirmation pass where before it never ran at all.

These are live-call effects of the dedup swap, not merely map bookkeeping, and were not called out
in §2.1. They are not asserted as bugs — a derived value backfilled under the retrying call, or a
stale rule finally getting to evaluate against confirmed-fresh state, are both plausibly cures for
staleness the retry exists to fix. But they were unrecorded, and the live gate (§5.3) must now
watch for them specifically.

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

### 3.2 Row volume is bounded — but the post-call judge is what actually scales

Today the write branch ends with `self._on_file[path] = value`, so a second identical extraction in
the same call re-enters the skip. Keying on `_recorded` reproduces that bound exactly: **a
CONSECUTIVE repeat of the value a path currently holds is skipped**, which is what the existing
`test_unchanged_value_is_recorded_once` pins.

Stated precisely, because an earlier draft of this section overstated it as "at most one row per
(path, distinct value) per call" — that is **not** what either key guarantees. Both `_recorded` and
the old `_on_file` hold only the LAST value written, so an oscillation `A → B → A` writes `A`
twice. Confirmed on the live gate (call `01a03d24-c701`): `insurance_information.group_name`
recorded `alpha → Alpha → alpha → Alpha` across four rows at evidence seqs 12/14/16/36. That is
unchanged from pre-fix behaviour — only the description was wrong. It also surfaced a PRE-EXISTING
extractor instability this change neither causes nor can fix — see follow-up F-f.

The worst case is a form that never captures a reference number across its full retry budget: every
repeated value is rewritten on each attempt, so ~170 paths × 5 attempts. Bounded and acceptable.
Nothing downstream assumes one row per `(form, path, call)`; the `fa_current_uq` partial-unique
index constrains *current* rows only, and `record_answer`'s demote-then-flush-then-insert maintains
it through the swap.

That bounds DB row growth, but row growth isn't the quantity that costs anything — the post-call
judge is. `post_call_eval.py:297-311` builds `observer_pairs` from EVERY current `ai_call` row this
call owns (`row.call_id == call_id and row.source == AnswerSource.AI_CALL.value`); `to_judge =
observer_pairs + kept` (`post_call_eval.py:389`) goes to `judge()`, which chunks at
`_JUDGE_CHUNK_SIZE = 50` (`llm.py:167`) and fires the chunks concurrently under a process-wide
`Semaphore(max_concurrency=8)` (`llm.py:209`) — each chunk resending the full transcript block.
Before this fix a confirmation-heavy retry wrote roughly zero new `ai_call` rows for confirmed
prefills, so the judge saw roughly one chunk; after it, every confirmed path becomes a row this
call owns, so a ~180-path form (the size the chunker was already sized for, `llm.py:164-166`) goes
from ~1 chunk to ~4: roughly 4x the judge tokens, and 4x the chunk-failure surface. A chunk not
salvaged after `_JUDGE_MAX_ATTEMPTS = 3` (`llm.py:163`) raises `PartialJudgeError`, which routes the
form to `EXCEPTION_REVIEW` / `ReviewReason.LLM_ERROR` (`post_call_eval.py:393`, `463-469`).

This is within the ceiling the chunker was already sized for, so it is **not a defect** — it is a
bounded, watch-this consequence. Watch judge latency, Vertex cost, and the `LLM_ERROR` rate on the
first retry-heavy day in production.

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

**In principle, this can cause the very redial it exists to prevent.** The judge is asked whether
the transcript SUPPORTS the extracted value, not merely whether it matches (`llm.py:125`) — and a
terse read-back confirmation ("Yes, that's correct") is weaker textual support than the rep stating
the value cold. `supported=False`, or `confidence` under the floor, turns a field that was
unconditionally satisfied as `intake` (`review.py:263-265`) into unsatisfied. If that tips
`unsatisfied_required_paths` non-empty, the form misses the `READY_FOR_REVIEW` park and falls
through to the fill-threshold gate instead (`post_call_eval.py:531-545`) — the same gate a genuine
shortfall would fail. So a confirmed value can, in principle, trigger a redial for a field the rep
already confirmed correctly.

Bounding this precisely:

* **Not a new category of exposure.** `policy_number` — required, `role="confirm"` — already
  depends on exactly this judge-support outcome on every `ibv_standard` form, with or without this
  change (§1.1): a confirm-role leaf is never satisfied by intake, only by a supported `ai_call`
  judgment, so `unsatisfied_required_paths` was already liable to include it whenever the judge
  withheld support. This fix broadens the judge-conditional population; it does not introduce the
  possibility of a judge-driven spurious redial where none existed before.
* **`completion_pct` is unaffected** — `completion_pct_v2` is value-presence only (`review.py:149`).
* **`verified_pct` only ever improves** — it routes through `is_call_confirmed`, which is the thing
  being fixed, and this change can only gain it satisfied paths, never lose one.
* **`retry_fill_threshold` defaults to 0.50** (`tenant.py:44`) — one spurious unsatisfied path out
  of a ~180-path form rarely tips an otherwise well-filled form below half.
* **No transient window on the normal path.** `evaluate_call` is "extract, persist, judge, and
  update status" in one transaction (`post_call_eval.py:148`), so `ai_supported` is written before
  `unsatisfied` is computed.
* **Confined to the eval path.** `unsatisfied_required_paths` and `retryable_required_paths` — the
  only callers of `is_field_satisfied` — run exclusively inside `evaluate_call`
  (`post_call_eval.py`). The fallback path's own gate, `resolve_ai_processing` →
  `load_verified_fraction` → `satisfied_required_fraction`, uses `is_call_confirmed`
  (`review.py:445`) instead of `is_field_satisfied`, so this asymmetry cannot reach the fallback
  path at all — see the corrected §7 F-c (an earlier pass at this document had that backwards).
* **Either outcome still parks in `EXCEPTION_REVIEW`.** A spurious redial converges on the same
  review queue a genuine one would: retries are capped (`sm.can_retry`), and every exhausted-retry
  or gate-declined branch of `evaluate_call` also ends in `EXCEPTION_REVIEW`
  (`RETRIES_EXHAUSTED` / `UNSATISFIED_UNASKABLE` / `AUTO_RETRY_DISABLED`) — the worst case is one
  extra call, never a state nothing reviews.
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
  engine (`observer.py:616`), which is the consumer its own docstring names. Assert on the engine's
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
* **Judge-verdict observation** (§3.5, same call): check the `field_evaluation` row's VERDICT for
  each confirmed prefill, not merely that the row landed — a `supported=False` or under-floor
  confirmation is the mechanism §3.5 now documents as a possible spurious-redial trigger, and it
  will not surface just because *some* `ai_call` row was written.
* **Rule-engine observation** (§2.3, same call, on a FOCUSED retry specifically): watch for an
  unexpected early `terminate_call` / `skip_to_task`, or a spurious `Contradiction` /
  `NumericConsistency` `ReAsk` — the rule engine now runs on a confirmation pass where it
  previously never did, and its input (`_on_file`) still carries the full prior-call/intake state.

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
* **F-c** — *corrected*: the fallback path (`post_call_eval_ready == False`) does leave every
  `ai_call` row permanently unjudged, but that is not where §3.5's consequence lands.
  `resolve_ai_processing` (`control_plane/post_call.py:95-110`) never calls
  `unsatisfied_required_paths`; its only gate is `load_verified_fraction` →
  `satisfied_required_fraction`, which uses `is_call_confirmed` (`review.py:445`) — a check that
  already required `source == ai_call` before this fix, so an intake row was already excluded, and
  an `ai_call` row with `ai_supported=None` fails the same check either way. The fraction was
  already pinned at whatever it was pre-fix; this change does not move it there. The demotion's
  real reach is the eval path (`evaluate_call`), the only caller of `unsatisfied_required_paths` /
  `retryable_required_paths` — see the corrected §3.5.
* **F-d** — every new `ai_call` row this fix writes for a value that agrees with a **non-canonical**
  legacy baseline opens a fresh dispute (§3.3's narrow exception, generalized): disputes are the
  only gate on the human `→ COMPLETED` transition (`api/v1/patient_forms.py:1604-1613`), and
  `canonical_answer` landed 2026-08-15 (`c59da163`) with no backfill migration — every form whose
  intake predates that commit carries non-canonical rows this change will re-record canonically and
  dispute. Likely a dev-data-only exposure (pre-prod at time of writing), but worth a data check
  before wide rollout.
* **F-f** — the extractor returns unstable spellings for free-text leaves across passes within a
  single call (`alpha`/`Alpha`, `5 cycles per year`/`5 cycle per year`), observed on the live gate.
  `canonical_answer` cannot fold either, because those leaves declare no authored literals to snap
  onto. Pre-existing and unrelated to this change, which only makes it visible by writing more
  rows. Costs churn rows and a longer superseded trail, not correctness: the last write wins and
  is the one the judge evaluates.
* **F-e** — `_push_recorded` now has exactly one call site (§2.4), which runs only after
  `record_answer` AND the bus emit both succeed. A Redis/bus failure mid-extraction-pass leaves
  that field "owed" for the rest of the call AND drops the remaining answers in that same
  extraction pass (the `for` loop in `TaskObserver._one_pass` propagates the exception, caught only
  by `_run_passes`'s outer handler). Self-heals on the next rep turn once a fresh pass re-extracts;
  only bites when Redis/the bus is already down.
