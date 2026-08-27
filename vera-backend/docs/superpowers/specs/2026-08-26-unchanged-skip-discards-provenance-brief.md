# The Observer's unchanged-skip discards provenance — pre-brainstorm design brief

**Date:** 2026-08-26
**Branch:** `fix/retry-calls` (fix lands HERE, per the product owner)
**Status:** NOT a spec yet. Evidence + options + open questions. Run
`superpowers:brainstorming` with the product owner before writing the spec.

## The defect, proven

A retry can only ever verify a field whose value **changed**. When the rep repeats the
previous answer — the strongest confirmation there is — the system discards it, and the field
stays `Unverified` forever.

`apps/agent_worker/src/agent_worker/observer.py:557`:

```python
if self._on_file.get(answer.field_path) == answer.value:
    # Unchanged — skip the write and the emit either way, so a rep merely confirming
    # a prefilled value still leaves no ai_call row (the INTAKE row stays current).
    if self._recorded.get(answer.field_path) != answer.value:
        self._push_recorded(answer.field_path, answer.value)
    return
```

`_on_file` is seeded from `plan.prefilled` (`observer.py:~407`). `call_plan.py:153` documents
that field as `{path: raw intake value}` — **the skip was designed for intake prefills** and
says so. On a retry, `prefilled` also carries **prior-call** values, so the skip fires outside
its own design intent.

### Evidence (three independent sources, 2026-08-25)

Form `01a03a0e-90ab-7eb1-92f6-3cf054130fc7`.
Call 1 `01a03a0e-a47b` (17:53–18:07): 102 rows, **no** reference captured → non-authoritative.
Call 2 `01a03a23-f255` (18:17–18:29): ran full top-to-bottom, **captured** the reference at
18:29:15 → authoritative — but wrote only **62** rows.

| field | intake | call 1 | call 2 said | result |
| --- | --- | --- | --- | --- |
| `benefit_coverage.coverage_type` | Family | Individual | Individual — same | **Unverified** |
| `benefit_coverage.plan_fund_type` | Fully Funded | Self Insured | Self Insured — same | **Unverified** |
| `benefit_coverage.benefit_year_type` | Calendar Year | Calendar Year | same | **Unverified** |
| `benefit_coverage.employer_support_size` | Large Group | Large Group | same | **Unverified** |
| `benefit_coverage.telehealth_covered` | Yes | — | **No** — changed | verified |
| `benefit_coverage.pcp_referral_required` | No | — | **Yes** — changed | verified |
| `insurance_information.group_name` | Umbrella Health | — | **Alpha** — changed | verified |

1. **Row ownership:** every `Unverified` field's `is_current` row is owned by call 1; every
   verified field's by call 2.
2. **Langfuse (ClickHouse):** in call 2's window there are 55 `vera.observer.answer_recorded`
   spans. The four `Unverified` paths are **absent**; the three verified paths are **recorded**.
   Perfect discrimination. (`observations.input`/`output` are NULL in ClickHouse — payloads
   live in MinIO — so span *presence* is the usable signal, not content.)
3. **Extraction is exonerated, and this is the discriminator.** Span absence alone cannot
   separate "extraction missed it" from "the Observer skipped it". But the skip path calls
   `_push_recorded` precisely so the controller learns the value — its comment says that
   otherwise "the field is owed for the rest of the call". Call 2 completed its tasks and fired
   `task_complete`, which only happens if the controller believed those fields answered. So
   extraction returned them, the controller was told, and **only the DB write was dropped.**

### Why it matters more than one field

`verified_pct` counts leaves confirmed by an authoritative call. Skipped fields keep their
non-authoritative provenance, so the number cannot converge; the retry gate sees a flat value
and redials; the retry re-asks; the rep repeats; the skip fires again. **It terminates only by
exhausting `max_retries`.** Observed live: form `01a039e6` burned all 5 retries with
`verified_pct` pinned at 91.95 while `completion_pct` reached 100.

Two corrections to the earlier record this brief supersedes:
- `2026-08-25-per-call-answers-review-ux-design.md` logs this (as review §15.3) as affecting
  `collected_per="call"` paths. **It affects every ordinary `ask` field.**
- That retry loop was previously called auto-retry "working as designed". It is this defect.

## Design options

**A — Stamp the plan with a "must re-record" path set (recommended starting point).**
The control plane already computes, at dispatch, the set of paths no authoritative call has
confirmed (that is what feeds `focus_paths`). Carry it on the `CallPlan`; in `_record_locked`,
skip only when the path is **absent** from that set.
*Pro:* the worker needs no notion of "authoritative"; the predicate is computed once where the
knowledge lives. *Con:* a new `CallPlan` field and a Redis plan-shape change.

**B — Skip only when the on-file value came from intake.**
Narrower restatement of the same idea: carry the *source* of each prefilled value rather than a
derived set. *Pro:* restores the skip's literal documented intent. *Con:* an answer confirmed
only by a non-authoritative call would still be skipped, so the bug survives in the exact
scenario above (call 1 wrote it, call 2 repeats it) — **probably disqualifying.**

**C — Drop the affected paths from `plan.prefilled`.**
What the earlier spec's deferred note suggested. **Reject:** `prefilled` also drives prompt
rendering and the readback ("I have X on file, can you confirm?"), so dropping values would
silently change what the bot says on the call.

**D — Never skip on a retry.**
Simplest. *Con:* loses the optimisation wholesale on the calls that need it most, and re-writes
intake-confirmed rows too.

## Open questions for the brainstorm

1. **Exact predicate.** "Not confirmed by an authoritative call" (option A) or "came from a
   call, not intake" (option B)? A is the one that actually fixes the observed case.
2. **Full calls too, or retries only?** A first call has no prior-call prefills, so A is a
   no-op there — but confirm rather than assume.
3. **Does a repeated-value write change dispute state?** `build_field_views` compares the
   current `ai_call` value against the most recent intake/human baseline. Writing a new
   `ai_call` row with an unchanged value should leave the dispute verdict identical — verify,
   because a spurious re-opened dispute would block form completion.
4. **There are TWO dedup layers.** Besides `observer.py:557` there is a no-op guard at
   `services/field_answers.py:110` which requires `current.call_id == call_id`. For a new call
   that is false, so it should not block the write — **verify before assuming the fix reaches
   the DB.**
5. **Row volume.** Every repeated answer now writes a row. Bound the growth on a 170-answer
   form across 5 retries and confirm nothing downstream assumes one row per (form, path, call).
6. **Does this subsume the `collected_per="call"` case** (rep name, reference number,
   `is_insurance_active`) recorded as review §15.3? It should — same skip, same fix.

## Constraints

- **PHI:** answer values are PHI. The plan carries them already; never log or span them.
- The agent worker has **no `FormSchemaDoc`** at runtime — anything schema-derived must be
  computed by the control plane and carried on the `CallPlan`.
- `vera_core` must not import from `apps/`.
- **Live gate required.** This changes voice-path behaviour, so `just check` is not sufficient:
  take a call where the rep repeats a prior non-authoritative answer and confirm the field
  flips to verified.
- Mutation-test every new assertion. This branch's history includes eight tests that passed
  with their feature deleted.
</content>
</invoke>
