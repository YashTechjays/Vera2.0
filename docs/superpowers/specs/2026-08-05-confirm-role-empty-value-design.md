# Confirm-role leaves with no value on file — design

**Date:** 2026-08-05
**Status:** approved for planning
**Scope:** backend (`vera_core.forms`, `agent_worker`, one control-plane intake call site)
plus the intake App Script's `coverage_type` vocabulary. No frontend change.

## Problem

On an infertility-treatment (IBV) call, the spouse's name and date of birth are confirmed
only when the representative says the coverage type is **Family**. When the clinic did not
enter spouse details at intake — the normal case, not an edge case — the bot either speaks
the literal string `{{value}}` or invents a spouse name and date of birth.

Two independent defects collide to produce it.

### Defect 1 — `{{value}}` has no substitution implementation

`{{value}}` is a reserved token (`forms/dsl.py:32-37`) that the prefill fuser passes through
verbatim by design (`forms/call_plan.py:308-309`). It is baked into the compiled task prompt
and reaches the LLM literally — visible in the committed snapshot
`tests/unit/forms/snapshots/ibv_insurance_basics.prompt.txt:33-35`:

```
   - Immediately after this answer:
     * If "Coverage Type" is "Family": confirm — Can we also check the spouse on the plan? I have the spouse listed as {{value}} — can you confirm that is correct?
```

The only thing that supplies a real value is the separate `# Values already on file` block,
assembled in `call_plan.py:323-331` and rendered at `agent_worker/plan_runtime.py:164-170`.
When intake omits the spouse name, `iter_leaf_answers` writes **no `field_answer` row at all**
(`forms/intake.py:112-125` skips empties), so that block has no spouse line. The leaf's
declared `default="N/A"` is never materialised into prefill — `default` only affects
intake-requiredness (`intake.py:71`) and completion percentage (`review.py:155`).

The model is therefore instructed to read back a value it was never given. Speaking the token
and inventing a value are the two ways that resolves.

The root cause is structural: the confirm sentence is baked at **compile** time (once per
schema version) but the value only exists at **fuse** time (once per form). The compiler has
no value; the fuser has no leaf. `{{value}}` is the seam between them and nothing closes it.

### Defect 2 — the Family gate is decided before its own question is asked

`_apply_gating()` runs once, in `PlanTaskAgent.on_enter` (`plan_runtime.py:215`), and nothing
re-runs it — `update_answers` (`plan_runtime.py:703`) does not. `is_applicable`
(`forms/conditions.py:88`) is boolean and a missing answer compares as `""`
(`conditions.py:37-40`), so "not yet known" is indistinguishable from "known false".

At entry to `insurance_basics`, `coverage_type` is unanswered, so `eq(coverage_type, "Family")`
is false and both spouse fields land under **"Excluded by the plan's gates — do NOT ask these,
whatever the task list says"** (`plan_runtime.py:136-139`) for the whole task, even after the
representative says "Family". The instructions then simultaneously say *confirm the spouse if
Family* and *never ask about the spouse*. A contradiction plus a missing value is what
produces improvisation.

### Why spouse, and not the other confirm leaves

There are four `role="confirm"` leaves in total:

| Leaf | Location | Value can be absent? |
|---|---|---|
| `spouse_partner_name` | `catalog/ibv_standard.py:182` | **Yes** |
| `spouse_partner_dob` | `catalog/ibv_standard.py:197` | **Yes** |
| `policy_number` (Policy / Member ID) | `catalog/ibv_standard.py:361` | No |
| `policy_number` (Policy / Member ID) | `catalog/disease_only.py:159` | No |

Both `policy_number` leaves are `system_fields` targets for `member_id`
(`ibv_standard.py:1013`, `disease_only.py:397`) carrying no `default`, so
`required_intake_fields` (`intake.py:52-73`) guarantees intake supplies them. The two spouse
leaves are the only confirm leaves whose value can legitimately be missing, because
`default="N/A"` exempts them from intake-requiredness while their gate — the coverage type —
is unknowable at intake time.

So the bug is live only for spouse, but latent for every confirm leaf. A blank member ID
would make the bot read back `{{value}}` or fabricate a member ID, which is a worse failure
than a fabricated spouse name.

## Decisions (settled with the user)

1. **Behaviour on absence:** the bot **asks openly**. The data is still needed and the clinic
   genuinely cannot know at intake whether coverage is Family, so a blank spouse is the normal
   case. The open question is **authored in the schema**, not improvised by the model — the
   spoken string is the product, and it must be reviewable.
2. **Scope:** schema-wide. Any confirm-role leaf with no value on file behaves correctly, in
   every schema. With only four confirm leaves this costs almost nothing and closes the latent
   member-ID hole.
3. **Gate:** fixed in the same change, via three-state classification. Live re-gating on
   answer updates (G2) is deliberately **deferred** — see Follow-ups.
4. **No in-flight / back-compat concern.** Nothing is in production; the project is dev-only
   at this point. `just seed-schemas` republishes and every form picks up the structural fix.
   An earlier draft added a "never speak text inside `{{ }}`" ground rule to `SCOPE_DISCIPLINE`
   as a backstop for forms pinned to an older `schema_version`; that justification is gone, so
   the rule is **dropped** rather than carried as prompt bloat.
5. **Enum vocabulary:** the App Script sheet is fixed at source **and** intake gains enum
   membership validation. See Component 7.

## Blast radius

| Layer | File | Change |
|---|---|---|
| Grammar / validator | `forms/dsl.py`, `forms/prompting.py` (`validate_prompt_document`) | Component 1 |
| Catalog | `catalog/ibv_standard.py`, `catalog/disease_only.py` | Component 2 |
| Prompt compiler | `forms/prompting.py` | Component 3 |
| Plan compiler / fuser | `forms/call_plan.py` | Components 4, 5, 6 |
| Agent runtime | `agent_worker/plan_runtime.py` | Component 5 |
| Intake | `forms/intake.py`, `control_plane/api/v1/patient_forms.py` | Component 7 |
| Intake sheet | `data/ibv_infertility_appscript.js` | Component 7 |
| Generated | `data/form_schemas/*.json`, prompt snapshots | `just compile-schemas`, re-record |

No frontend change (see Component 1, rule 2).

## Component 1 — Grammar and validator (`forms/dsl.py`)

`FieldPrompt` already carries both `ask` and `confirm` (`dsl.py:268-271`); no model change is
needed. The validator gains three rules:

1. A `confirm`-role leaf must supply **both** `prompt.confirm` (the read-back, containing
   `{{value}}`) and `prompt.ask` (the open question used when nothing is on file). Today only
   `prompt.confirm` is required.
2. `{{value}}` is legal **only** inside a confirm-role leaf's `prompt.confirm`. It is rejected
   in `prompt.ask`, in any other leaf's prompts, and in task-level text — the latter also in
   `validate_prompt_document` (`prompting.py:452-489`), which today exempts it as a reserved
   token, so a tenant persona override could carry one.

   This breaks no existing content: `{{value}}` is authored in exactly the four confirm
   prompts listed in Component 2 and nowhere else in either catalog. It also makes the backend
   enforce a promise the authoring UI already makes — `PlaceholderPicker.tsx:130` tells authors
   `{{value}}` "belongs to schema field prompts only — it is not valid in session or task
   text", and never offers it as an insertable token. Hence no frontend change.
3. Leaf-level `prompt.ask` / `prompt.confirm` gain placeholder validation, which they have
   none of today — `dsl.py:652-668` validates task-level text only, which is why a bad token in
   a field prompt reaches the wire silently. Tokens must resolve to a `system_fields` key or a
   root-anchored leaf path, exempting `RESERVED_PLACEHOLDER_TOKENS`, and the existing
   `_MALFORMED_PLACEHOLDER_RE` check (`dsl.py:49`) applies so `{{ value }}` and `{value}` fail
   at compile.

`RESERVED_PLACEHOLDER_TOKENS` keeps both members. `{{value}}` stays reserved so the
path-resolution validator does not try to resolve it as a leaf path; what changes is that it
is now *consumed at fuse time* rather than passed through to the model.

## Component 2 — Catalog: authored ask fallbacks

Each of the four confirm leaves gains an `ask` alongside its `confirm`:

| Leaf | `ask` |
|---|---|
| `ibv_standard` `spouse_partner_name` | `"Can we also check the spouse on the plan? Can I get the spouse's full name?"` |
| `ibv_standard` `spouse_partner_dob` | `"And what is the spouse's date of birth?"` |
| `ibv_standard` `policy_number` | `"Can I get the member ID for this policy?"` |
| `disease_only` `policy_number` | `"Can I get the member ID for this policy?"` |

The two `policy_number` asks are defensive — intake guarantees those values — but the
validator requires them and they cost nothing.

Final wording is subject to the TTS probe (see Verification); the strings above are the
starting point, not the sign-off.

## Component 3 — Prompt compiler: the confirm slot (`forms/prompting.py`)

`prompting.py` stops baking the confirm sentence and emits a path-keyed slot instead, so the
per-form decision moves to the fuser. Two call sites render confirm text today:

- `prompting.py:358-363` — `confirm_immediate` leaves, anchored after their trigger question.
- `prompting.py:419-425` — end-of-task confirms.

Both emit `{{confirm:<root-anchored path>}}`. The compiled template becomes:

```
   - Immediately after this answer:
     * If "Coverage Type" is "Family": {{confirm:sections.patient_information.spouse_partner_name}}
```

The `confirm — ` / `ask — ` verb prefix moves **into** the slot expansion, since the fuser is
what knows which one applies. Consequently the end-of-task header at `prompting.py:420`
changes from `"Before finishing this task, confirm:"` to `"Before finishing this task:"` — the
list can now mix confirms and asks, so a header asserting "confirm" would be false.

The slot uses its own regex (`CONFIRM_SLOT_RE = r"\{\{confirm:([\w.]+)\}\}"`), not
`PLACEHOLDER_RE` (`dsl.py:31`), whose `[\w.]+` character class cannot match the colon. The
existing token grammar is therefore untouched.

This adds a second token *form* to the compiled output (today every token is a bare name or a
path). That is deliberate: a bare `{{sections.…spouse_partner_name}}` would reuse the existing
form, but then the fuser could not distinguish "substitute a value here" from "swap the whole
sentence for the ask variant", which is the entire point.

## Component 4 — Fuser: two-pass expansion (`forms/call_plan.py`)

`PrefillFuser.__init__` already receives `doc` and walks it for `_titles` (`call_plan.py:271`),
so it builds `{path: (confirm_text, ask_text)}` in the same pass at no extra cost.

`fuse` applies **two ordered passes** to each task prompt — sequential, not nested:

- **Pass 1 — `expand_slots`.** Replaces each `{{confirm:<path>}}` with either
  `confirm — <prompt.confirm with {{value}} substituted>` or `ask — <prompt.ask>`, depending
  on whether `_render_value(values.get(path))` returns a value. Because the leaf is known
  right here, its `{{value}}` is substituted directly through the existing `_render_value` /
  `_speak_iso_date` helpers (`call_plan.py:233-254`) rather than by recursing into
  `PLACEHOLDER_RE`.
- **Pass 2 — `hydrate`** (existing, `call_plan.py:301-321`). Resolves `system_fields` and path
  tokens and runs `_dedupe_honorifics`, now also covering the text pass 1 expanded — so a
  prefilled `"Dr. Jane"` inside a confirm sentence still collapses correctly.

Pass 1 consumes every legal `{{value}}`, so pass 2's reserved-token branch never sees one, and
a stray token anywhere else is counted by the existing `unresolved` warning
(`call_plan.py:356-362`) instead of shipping silently.

Pass 1 applies to `task.prompt` only. Slots are emitted exclusively by the two question-line
renderers, so they cannot appear in `session.persona` / `goal` / `base_instructions` or in a
task `intro` / `outro`.

Fused output, spouse name on file:

```
     * If "Coverage Type" is "Family": confirm — Can we also check the spouse on the plan?
       I have the spouse listed as Jane Doe — can you confirm that is correct?
```

Fused output, nothing on file:

```
     * If "Coverage Type" is "Family": ask — Can we also check the spouse on the plan?
       Can I get the spouse's full name?
```

This also fixes the retry path. `focus_call_plan` (`call_plan.py:190-208`) deliberately clears
`on_file_values`, so on **every** focused retry today the `{{value}}` token is guaranteed
unbacked. After this change the value rides inline in the task prompt, so retries read back
correctly.

`# Values already on file` (`plan_runtime.py:164-170`) stays as it is. It remains useful
background; it is simply no longer the only channel carrying the value.

## Component 5 — Three-state gating (`agent_worker/plan_runtime.py`)

**No new condition helper is needed.** `dsl.condition_field_paths(cond, shared, depth=0) ->
Iterator[str]` (`dsl.py:106-122`) already yields every leaf path a condition references with
shared `ref`s expanded and a recursion guard, and is already used for exactly this kind of
reachability question by the `confirm_immediate` anchor validator (`dsl.py:679`) and by
`prompting.py:266,286`. Reuse it.

So `conditions.py` is **not** modified: `evaluate` semantics are untouched and no mirroring is
needed in `vera-frontend/src/lib/ibv/conditions.ts`. The whole of this component lands in
`plan_runtime.py`.

`PlanRunController` then partitions a task's fields three ways instead of two
(`plan_runtime.py:778-799`):

Classification is per field, over its whole gate chain (`leaf_gates` collects section-to-leaf
`applicable_when` into a tuple, `conditions.py:67-85`):

- **applicable** — every gate in the chain evaluates true. Unchanged.
- **excluded** — some gate is false **and** every path *that gate* references is answered.
- **conditional** — neither of the above: not applicable, and no single gate is decidably false.

Because `is_applicable` is `all(gates)` (`conditions.py:88-89`), one decidably-false gate makes
the entire chain decidably false — so exclusion is quantified over a *single* gate, not over
every path in the chain. A field gated on both an answered-false condition and an unanswered
one is correctly **excluded**, not conditional.

"Answered" is the existing `_is_answered` predicate (`plan_runtime.py:812-814`): present and
not blank.

`_gating_block` (`plan_runtime.py:118-140`) grows a third section:

```
# Conditional on this call — ask only if the condition holds
- Spouse / Partner Name — only if "Coverage Type" is "Family"
- Spouse / Partner Date of Birth — only if "Coverage Type" is "Family"
```

The condition prose comes from a new `gate_text: str | None` on `PlanFieldDescriptor`
(`call_plan.py:71-83`), rendered at compile time in `compile_call_plan` with the
`build_condition_renderer(doc)` that `prompting.py:182` already uses, joined across the chain by
the same `_join_gates` helper (`prompting.py:153`). Compile-time and deterministic, so the
gating block and the task prompt state the condition in **identical** words — the consistency is
the point. No runtime titles map is added to `CallPlan`.

`gate_text` holds the rendered condition **only** — e.g. `"Coverage Type" is "Family"` — and is
`None` for an ungated field. `_gating_block` supplies the `— only if ` prefix, so the same
descriptor stays reusable if another consumer wants the bare condition.

Two knock-on adjustments:

- `_skip_when_nothing_applies` (`plan_runtime.py:231-250`) must treat a task holding only
  *conditional* fields as still live. Silently skipping an undecided task would be a new bug.
- `gap_fields` (`plan_runtime.py:801-810`) keeps counting only genuinely applicable fields, so
  the end-of-call gap pass does not chase conditionals that resolved false.

With the gate honest, control returns to the compiled task prompt's own
`If "Coverage Type" is "Family"` conditional — the same mechanism every other gated question
already uses (`prompting.py:337-344`).

## Component 6 — Hardening: `_render_value` drops `"N/A"`

`_render_value` (`call_plan.py:233-242`) returns `"N/A"` as a spoken value. `ivr_selection._spoken_value`
(`services/ivr_selection.py:75-88`) already learned this lesson and suppresses it; the fuser
never mirrored it. Mirror it.

This composes with Component 4 exactly right: a clinic that types `"N/A"` into the spouse cell
means *"I don't have this"*, so the slot resolves to the **ask** variant instead of reading
"N/A" aloud. `"N/A"` is never a value worth speaking — it is the `inapplicable_value` marker
and the declared `default` — so no legitimate read-back is suppressed.

It also affects `known_information` and `on_file_values` (`call_plan.py:323-331`), where an
`"N/A"` line is noise. Dropping it there is correct too.

## Component 7 — Enum vocabulary: sheet fix plus intake validation

The intake sheet's coverage-type dropdown uses `PT/Spouse` — `ibv_infertility_appscript.js:621`
compares cell `AD19` against `"pt/spouse"` — while the schema declares
`values=["Individual", "Family"]` (`catalog/ibv_standard.py:423-427`) and the gate is
`eq(coverage_type, "Family")` (`ibv_standard.py:1103`). Line 38 maps `AD19` straight into
`coverage_type` with no translation, and `getFormattedValue` performs none.

Today that is merely a gate that never opens. **Under Component 5 it becomes worse:**
`coverage_type` *is* answered, so the gate is decidably false and the spouse questions are
hard-**excluded** rather than landing in `conditional`. The garbage prefill would be more
damaging after the fix than before it, so this cannot stay out of scope.

Two changes:

1. **Fix the sheet at source.** Map `AD19` to the declared enum in
   `data/ibv_infertility_appscript.js` — the field map at line 38 and the `"pt/spouse"`
   comparison at line 621. This eliminates the known instance and needs no backend change.
2. **Add enum membership validation at intake.** There is none today: `intake.py` never reads
   `leaf.values`, and the intake pipeline
   (`control_plane/api/v1/patient_forms.py:224-235`) validates unknown paths → 422, phone
   prefix → normalised, dates → parsed or 422, and nothing else. So `"PT/Spouse"` currently
   lands in a `field_answer` row as a legal-looking answer.

   Add `validate_enum_answers(answers, doc)` to `intake.py` beside `normalize_phone_answers`
   and `normalize_date_answers`, wired into `patient_forms.py` as a **422** next to
   `_normalize_date_answers_or_422`. An enum leaf's value must be one of its declared `values`
   or `special_values`. Rejecting rather than silently dropping is the right default now that
   there are no production clinics to break: a schema that declares `values` and then accepts
   anything is a validator gap, and it is why this bug could reach a live call at all.

   Per `intake.py`'s module docstring, validation errors carry **paths only, never values** —
   an out-of-enum value may be PHI.

## Failure modes

- **Slot path missing from the fuser's map** (should be impossible — compile-time guaranteed):
  emit the **ask** variant, never the confirm. An open question is never wrong; a fabricated
  read-back is. Warn with a count only, never content.
- **Validator rejection:** fails `just compile-schemas` loudly, before anything ships.
- **Enum rejection at intake:** 422 with field paths only. The form is not created, which is
  the intended loud failure.

## Testing

Unit, `tests/unit/forms/`:

- Fuse a confirm slot with the value present, absent, literal `"N/A"`, and an ISO date
  (proving `_speak_iso_date` still runs inside the slot expansion).
- Validator rejects a confirm leaf missing `prompt.ask`; rejects `{{value}}` in `prompt.ask`
  and in task-level text; rejects `{{ value }}` and `{value}` in a leaf prompt.
- `condition_fields` over `eq`, `all`, `any`, `not`, and `ref` (resolving through
  `shared_conditions`).
- `validate_enum_answers` accepts declared `values` and `special_values`, rejects others,
  and reports paths only.

Unit, `apps/agent_worker/tests/unit/test_plan_runtime.py`:

- The three-way partition: unanswered gate → conditional, answered-false → excluded,
  answered-true → applicable.
- `_gating_block` renders the conditional section with `gate_text`.
- `_skip_when_nothing_applies` treats an only-conditional task as live.

Snapshots:

- Re-record `tests/unit/forms/snapshots/ibv_insurance_basics.prompt.txt` — the template now
  carries slots.
- **Add a fused snapshot for both branches** (value on file / nothing on file). Today's
  fixture covers the template only; the fused snapshot is the test that actually proves this
  bug dead.

## Verification beyond pytest

`vera-backend/CLAUDE.md` is explicit that a change to spoken output is not verified by
`pytest` — the assertions are on strings and the defect lives in the audio. This is squarely a
spoken-output change.

1. `just check` — the full gate (ruff check **and** `format --check`, mypy --strict, pytest).
2. `just compile-schemas` (freshness test enforces no drift), then `just seed-schemas` to
   publish the new `schema_version`.
3. `uv run --no-project --with certifi python scripts/tts_probe.py --set verbatim` on both
   branches, to sign off on the shipped strings rather than a hand-typed approximation.
4. Eval-harness scenario (`apps/agent_worker/tests/evals/`, `-m evals`, opt-in — **never**
   added to `just check`): `coverage_type=Family` with blank spouse prefill, asserting the
   transcript contains no `{{` and no invented spouse name.
5. **A live call.** `just check` gates the merge; the live call gates the ship.

Per the repo-wide rule, run the **code-simplifier** plugin on the change before committing,
then re-run `just check`.

## Out of scope / follow-ups

- **G2, live re-gating.** Re-running `_apply_gating` from `update_answers`
  (`plan_runtime.py:703`) when the partition changes would let the spouse fields move from
  conditional to excluded the moment the representative says "Individual". Strictly more
  correct, but it needs an async hop out of a sync controller method plus change-detection to
  avoid rewriting instructions on every extraction tick. Its cost lands in the live agent loop
  and deserves its own live-call verification, so it ships separately. Component 5 removes the
  contradiction without it.
- **`default="N/A"` and completion percentage.** `review.py:155` counts a leaf with a declared
  `default` as filled, so an unfilled spouse name inflates the form's completion percentage.
  Same root cause — `default` doing double duty — but a separate concern from the spoken
  prompt.
- **Application-level PHI column encryption** remains deferred per
  `vera_core/CLAUDE.md`; nothing here changes that.
