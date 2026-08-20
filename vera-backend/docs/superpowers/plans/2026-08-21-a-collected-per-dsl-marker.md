# Plan A — `collected_per` DSL marker

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the DSL a way to say "this answer describes one call, not the form", so retry
scoping, dispute suppression and per-attempt rendering stop being hardcoded to
`plan.tasks[0]` and `rep_call_reference_number_field`.

**Architecture:** One optional enum on `Leaf`, `Group` and `Section` (`collected_per`), one
resolution helper on `FormSchemaDoc` that applies most-specific-wins inheritance to `ask`-role
leaves, one `Leaf` validator, and the marker declared on the representative section of both
catalogs. Nothing reads the helper yet — Plan B is the first consumer.

**Tech Stack:** Python 3.12, pydantic v2, pytest, ruff, mypy `--strict`, `just` recipes. All
code in `packages/vera_core/src/vera_core/forms/` — pure and DB-free.

**Spec:** `vera-backend/docs/superpowers/specs/2026-08-21-retry-call-scoping-design.md`

## Global Constraints

- Every command runs from `vera-backend/`.
- `vera_core/forms/` stays **pure and DB-free** — no I/O, deterministic. The agent worker has no
  `FormSchemaDoc` at runtime.
- **No `dsl_version` bump** (the field is optional), **no backfill migration, and no runtime
  fallback**. There is no production data — the database holds two seeded test forms — so this
  branch simply requires the schema version this plan publishes (spec D7). Two guards in this plan
  carry that: Task 3's check that no `patient_form` is pinned to a demoted version, and Task 2's
  catalog tests, which fail CI if a schema is authored without the marker.
- **No document-level validator may require the marker.** Combined with the `role="ask"` rule it
  makes existing fixtures unconstructible: ~12 inline test documents across ten files point
  `rep_call_reference_number_field` at an arbitrary filler leaf, and `test_intake.py` /
  `test_export_form_sheet.py` point it at `sections.patient_information.patient_name`, which is
  `role="context"`. The guard is a catalog test instead (spec D1).
- **No frontend change.** `vera-frontend/src/lib/ibv/types.ts` is a UI-rendering subset that
  excludes `tasks`, `tags`, `prompt`, `flow_rules`; `collected_per` is voice-only until Plan E.
- Code style: PEP 695 type params (`class Foo[T]`, `def f[T]`) — ruff rejects
  `Generic[T]`/`TypeVar`.
- Never log a field value.
- `just check` verbatim (ruff check **and** format --check, mypy --strict, pytest), then
  `/simplify`, then `just check` again, before claiming done.

---

## File Structure

- **Modify** `packages/vera_core/src/vera_core/forms/dsl.py`
  - `CollectedPer` type alias beside `LeafRole` / `SectionRole` (line ~59-61)
  - `collected_per` field on `Leaf` (class at line 288), `Group` (line 341), `Section` (line 387)
  - `Leaf._coherent` validator rule (line 310)
  - `FormSchemaDoc.collected_per_call_paths()` in the "documented walk helpers" block (after
    `collection_paths`, line 543)
- **Modify** `packages/vera_core/src/vera_core/forms/authoring.py:364` — `enum_ask` gains a
  `collected_per` parameter (needed for the one leaf-level mark)
- **Modify** `packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py` — mark the
  `insurance_representative` (line 967) and `patient_verification` (line ~995) sections
- **Modify** `packages/vera_core/src/vera_core/forms/catalog/disease_only.py` — mark the
  `representative_details` section (line 343) and the
  `coverage_summary.disease_coverage_active` LEAF (line ~193)
- **Modify** `scripts/seed_retry_form.py` — drop the `Call.call_reference_no` write (dead column)
- **Modify** `data/form_schemas/ibv_form_standard_v2.json`,
  `data/form_schemas/disease_only_verification.json` — regenerated, never hand-edited
- **Modify** `scripts/seed_retry_form.py` — drop the `Call.call_reference_no` write (dead column)
- **Test** `tests/unit/forms/test_schema_dsl.py` — resolution, inheritance, validator, and the
  catalog guards

**Interfaces:**

- Produces, for Plan B and Plan E:

```python
CollectedPer = Literal["form", "call"]

class Leaf(_Model):
    collected_per: CollectedPer | None = None   # None = inherit; document default "form"

class Group(_Model):
    collected_per: CollectedPer | None = None

class Section(_Model):
    # declared BEFORE `fields` so the artifact reads with the marker beside `title` — see
    # Task 1 Step 3.
    collected_per: CollectedPer | None = None

class FormSchemaDoc(_Model):
    def collected_per_call_paths(self) -> frozenset[str]: ...
```

---

### Task 1: the `collected_per` field and its inheritance

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/dsl.py`
- Test: `tests/unit/forms/test_schema_dsl.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `CollectedPer`, `Leaf.collected_per`, `Group.collected_per`,
  `Section.collected_per`, `FormSchemaDoc.collected_per_call_paths()`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/forms/test_schema_dsl.py`. It already has `minimal_doc(**overrides) ->
dict[str, Any]` ("smallest valid document; tests mutate copies of it", line 44) and imports
`pytest`, `ValidationError` and `FormSchemaDoc` — reuse all of it. The fixture below has been
run against the current document validator in all four shapes (flat, with-confirm, grouped) and
validates.

```python
def _sections(
    *,
    section_mark: str | None = None,
    group_mark: str | None = None,
    leaf_mark: str | None = None,
    grouped: bool = False,
    with_confirm: bool = False,
) -> dict[str, Any]:
    """`minimal_doc`'s `basics` section, reshaped for marker resolution.

    `plan_type` always sits directly under the section (so it reads the SECTION marker);
    `rep_name` moves under a group when `grouped`, so the group marker has something to govern.
    """
    rep_name: dict[str, Any] = {
        "type": "text",
        "title": "Representative Name",
        "role": "ask",
        "required": True,
        "prompt": {"ask": "May I have your name?"},
    }
    if leaf_mark is not None:
        rep_name["collected_per"] = leaf_mark
    inner: dict[str, Any] = {"rep_name": rep_name}
    if with_confirm:
        inner["policy_number"] = {
            "type": "text",
            "title": "Policy Number",
            "role": "confirm",
            "prompt": {"ask": "What is the policy number?", "confirm": "I have {{value}}."},
        }
    plan_type: dict[str, Any] = {
        "type": "text",
        "title": "Plan Type",
        "role": "ask",
        "required": True,
        "prompt": {"ask": "What type of plan is this?"},
    }
    if grouped:
        group: dict[str, Any] = {"type": "group", "title": "Rep Block", "fields": inner}
        if group_mark is not None:
            group["collected_per"] = group_mark
        fields: dict[str, Any] = {"plan_type": plan_type, "block": group}
    else:
        fields = {"plan_type": plan_type, **inner}
    section: dict[str, Any] = {"title": "Basics", "fields": fields}
    if section_mark is not None:
        section["collected_per"] = section_mark
    return {"basics": section}


def _doc(**kwargs: Any) -> FormSchemaDoc:
    return FormSchemaDoc.model_validate(minimal_doc(sections=_sections(**kwargs)))


class TestCollectedPer:
    """`collected_per` resolution: most specific wins, ask-role only."""

    def test_defaults_to_form_so_nothing_is_call_scoped(self) -> None:
        assert _doc().collected_per_call_paths() == frozenset()

    def test_section_marker_reaches_its_ask_leaves(self) -> None:
        assert _doc(section_mark="call").collected_per_call_paths() == frozenset(
            {"sections.basics.plan_type", "sections.basics.rep_name"}
        )

    def test_section_marker_skips_a_confirm_leaf(self) -> None:
        """A confirm leaf is on file precisely to be read back — `gating_seed` keeps confirm
        prefills, so marking one call-scoped would recite last call's value, not collect one."""
        paths = _doc(section_mark="call", with_confirm=True).collected_per_call_paths()
        assert "sections.basics.policy_number" not in paths

    def test_leaf_declaration_overrides_its_section(self) -> None:
        assert _doc(section_mark="call", leaf_mark="form").collected_per_call_paths() == frozenset(
            {"sections.basics.plan_type"}
        )

    def test_nearest_group_wins_over_the_section(self) -> None:
        doc = _doc(section_mark="call", group_mark="form", grouped=True)
        assert doc.collected_per_call_paths() == frozenset({"sections.basics.plan_type"})

    def test_leaf_wins_over_its_group(self) -> None:
        doc = _doc(group_mark="form", leaf_mark="call", grouped=True)
        assert doc.collected_per_call_paths() == frozenset({"sections.basics.block.rep_name"})

    def test_call_on_a_confirm_leaf_is_rejected(self) -> None:
        sections = _sections(with_confirm=True)
        sections["basics"]["fields"]["policy_number"]["collected_per"] = "call"
        with pytest.raises(ValidationError, match='requires role="ask"'):
            FormSchemaDoc.model_validate(minimal_doc(sections=sections))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -k CollectedPer -v`
Expected: FAIL — `TypeError`/`ValidationError` on the unknown `collected_per` keyword
(`_Model` sets `extra="forbid"`), and `AttributeError` on `collected_per_call_paths`.

- [ ] **Step 3: Add the type alias and the three fields**

In `dsl.py`, beside the existing role aliases (line ~59-61):

```python
SectionRole = Literal["collect", "context", "ui_only"]
LeafRole = Literal["ask", "confirm", "context", "readonly", "input"]
LeafType = Literal["text", "enum", "date", "currency", "percent", "integer", "phone"]
# Whether an answer describes the FORM (a benefit fact — collect it once) or the CALL that
# produced it (a rep's name, a call reference number — collect it on every call). `None` on a
# node means "inherit"; the document-level default is "form". A leaf that defaulted to "form"
# instead of None would silently override its section's declaration.
CollectedPer = Literal["form", "call"]
```

Add the field to each of the three models, keeping declaration order (which is the canonical
key order — see the `Leaf` docstring) by appending it last:

```python
class Leaf(_Model):
    ...
    description: str | None = None
    collected_per: CollectedPer | None = None
```

```python
class Group(_Model):
    ...
    description: str | None = None
    collected_per: CollectedPer | None = None
    fields: dict[str, FormField]
```

```python
class Section(_Model):
    ...
    ui: Ui | None = None
    # BEFORE `fields`, so the compiled artifact reads with the marker next to `title`/`description`
    # rather than stranded after a 73-leaf block.
    collected_per: CollectedPer | None = None
    fields: dict[str, FormField]
```

Same position on `Group`, for the same reason. On `Leaf` there is no `fields` key, so it goes last,
after `description`.

- [ ] **Step 4: Add the `Leaf` validator rule**

In `Leaf._coherent` (line 310), alongside the existing role/prompt coherence checks:

```python
        if self.collected_per == "call" and self.role != "ask":
            raise ValueError('collected_per="call" requires role="ask"')
```

- [ ] **Step 5: Add the resolution helper**

In `FormSchemaDoc`, in the "documented walk helpers" block right after `collection_paths`:

```python
    def collected_per_call_paths(self) -> frozenset[str]:
        """Root-anchored paths of every `ask`-role leaf whose effective `collected_per` is
        "call" — asked on every call, whatever is on file.

        Most specific declaration wins: the leaf, then its enclosing groups nearest-first, then
        its section, then the document default of "form". Restricted to `ask` because a
        `confirm` leaf is on file precisely to be read back (`gating_seed` keeps confirm
        prefills) and every other role is never spoken, so a section marker on a mixed section
        reaches its ask leaves and leaves the rest alone.
        """
        nodes = dict(self._iter_fields())
        return frozenset(
            path
            for path, leaf in self.leaf_items()
            if leaf.role == "ask" and self._effective_collected_per(path, leaf, nodes) == "call"
        )

    def _effective_collected_per(
        self, path: str, leaf: Leaf, nodes: Mapping[str, FormField]
    ) -> CollectedPer:
        """`leaf`'s marker resolved outward through its groups and section."""
        if leaf.collected_per is not None:
            return leaf.collected_per
        parts = path.split(".")
        # Enclosing groups, nearest first. parts[0:2] is `sections.<key>`, so stop above it.
        for cut in range(len(parts) - 1, 2, -1):
            parent = nodes.get(".".join(parts[:cut]))
            if isinstance(parent, Group) and parent.collected_per is not None:
                return parent.collected_per
        section = self.sections.get(parts[1])
        if section is not None and section.collected_per is not None:
            return section.collected_per
        return "form"
```

`dsl.py` line 23 imports only `Iterator` from `collections.abc`, so widen that exact line —
do not add a second import line:

```python
from collections.abc import Iterator, Mapping
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -k CollectedPer -v`
Expected: PASS, 7 tests.

- [ ] **Step 7: Confirm the field is purely additive**

Nothing may stop parsing: `schema_version` rows are immutable, forms pin them forever, and retry
dispatch re-validates a form's own pinned document. Prove it against the real files rather than
assuming:

Run:
```bash
uv run python -c "
import json, pathlib
from vera_core.forms.dsl import FormSchemaDoc
for f in sorted(pathlib.Path('data/form_schemas').glob('*.json')):
    if f.name == 'manifest.json':
        continue
    doc = FormSchemaDoc.model_validate(json.loads(f.read_text()))
    print(f.name, '->', len(doc.collected_per_call_paths()), 'call-scoped paths')
"
```
Expected: both schema files parse, each reporting `0 call-scoped paths` (the catalogs are not
marked until Task 2).

- [ ] **Step 8: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py tests/unit/forms/test_schema_dsl.py
git commit -m "feat(dsl): collected_per marker for per-call answers"
```

---

### Task 2: mark both catalogs and guard the reference-number leaf

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py:967`
- Modify: `packages/vera_core/src/vera_core/forms/catalog/disease_only.py:343`
- Modify: `data/form_schemas/ibv_form_standard_v2.json` (regenerated)
- Modify: `data/form_schemas/disease_only_verification.json` (regenerated)
- Test: `tests/unit/forms/test_schema_dsl.py`

**Interfaces:**
- Consumes: `FormSchemaDoc.collected_per_call_paths()` from Task 1.
- Produces: both catalogs resolve their `rep_call_reference_number_field` to `"call"`.

- [ ] **Step 1: Write the failing catalog test**

This is the guard that a future schema cannot lose its reference capture. It lives here, not in
a document validator, because a validator would reject rows published before this change (see
Global Constraints). Mirror the style of `test_no_catalog_uses_an_end_of_task_confirm`.

Append to `tests/unit/forms/test_schema_dsl.py`. Both builders — `build_ibv_standard` and
`build_disease_only` — are already imported at the top of the file (lines 11-12), and the
`for build in (build_ibv_standard, build_disease_only):` shape is the module's established
style for catalog-wide assertions (see `test_no_catalog_uses_an_end_of_task_confirm`).

```python
def test_every_catalog_marks_its_reference_number_leaf() -> None:
    """A retry that never logs its OWN reference number breaks the next retry's focus gate
    (`has_call_reference`), and the wrap-up task survives a focused retry only because it holds
    a call-scoped leaf. Nothing else in the system can infer that from the document.

    Enforced here rather than in a `FormSchemaDoc` validator: that runs on every dispatch
    against the PINNED schema version, and rows published before this marker existed have no
    declaration to find.
    """
    for build in (build_ibv_standard, build_disease_only):
        doc = build()
        assert doc.rep_call_reference_number_field in doc.collected_per_call_paths(), (
            f"{doc.insurance_type}: rep_call_reference_number_field is not collected_per=call"
        )


def test_every_catalog_marks_the_rep_name_beside_it() -> None:
    """The rep's name is as per-call as the reference number: a retry that keeps the prior rep's
    name attributes this call's answers to someone who was never on it."""
    for build in (build_ibv_standard, build_disease_only):
        doc = build()
        section = doc.rep_call_reference_number_field.rsplit(".", 1)[0]
        call_scoped = doc.collected_per_call_paths()
        rep_names = [
            path
            for path, _leaf in doc.leaf_items()
            if path.startswith(f"{section}.") and path.endswith(".rep_name")
        ]
        assert rep_names, f"{doc.insurance_type}: no rep_name leaf beside the reference number"
        for path in rep_names:
            assert path in call_scoped, f"{path} is not collected_per=call"


def _always_run_task_keys(doc: FormSchemaDoc) -> list[str]:
    """Tasks a focused retry must keep, derived per spec D2: it has a `collected_per="call"`
    descendant, or it collects nothing at all."""
    call_scoped = doc.collected_per_call_paths()
    keys: list[str] = []
    for task in doc.tasks:
        collects = [
            path
            for path, leaf in doc.leaf_items()
            if path.split(".")[1] in task.sections and leaf.role in COLLECTED_ROLES
        ]
        if not collects or call_scoped.intersection(collects):
            keys.append(task.task_key)
    return keys


def test_a_task_carrying_the_calls_opening_is_always_retained() -> None:
    """`opening_line` speaks the FIRST-ENTERED task's `intro`, so a schema that puts its greeting
    and recording/identity disclosure there loses both entirely if a focused retry drops that task.
    The task survives only because it happens to hold a call-scoped leaf — nothing in the code
    connects those two facts, so pin it here.

    Vacuous for a schema whose opening task has no `intro` (`disease_only` starts straight into
    questions): nothing rides on it, and dropping it when it owes nothing is correct.
    """
    for build in (build_ibv_standard, build_disease_only):
        doc = build()
        if doc.tasks[0].intro is None:
            continue
        assert doc.tasks[0].task_key in _always_run_task_keys(doc), (
            f"{doc.insurance_type}: the opening task carries an intro but is not retained by a "
            "focused retry — its greeting and recording disclosure would be dropped"
        )


def test_the_closing_task_is_always_retained() -> None:
    """`_closing_task_index` is `len(plan.tasks) - 1` and the gap pass stops before it, so the
    closer is where the reference number and the goodbye happen. Dropping it would break the NEXT
    retry's focus gate as well as this call's sign-off."""
    for build in (build_ibv_standard, build_disease_only):
        doc = build()
        assert doc.tasks[-1].task_key in _always_run_task_keys(doc), doc.insurance_type
```

`COLLECTED_ROLES` needs importing from `vera_core.forms.dsl` if this module does not already have
it.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -k "marks_its_reference_number or marks_the_rep_name" -v`
Expected: both FAIL — `rep_call_reference_number_field is not collected_per=call` on
`infertility_treatment` (whichever catalog the loop reaches first).

- [ ] **Step 3: Mark the IBV standard sections**

`packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py:967`:

```python
        "insurance_representative": Section(
            title="Insurance Representative",
            # Both leaves describe THIS call, not the form: a retry must capture its own rep
            # name and reference number, and the review UI must not diff them against the form.
            collected_per="call",
            fields={
                "rep_name": text_ask(
                    "Representative Name",
                    "May I have your first name and last name initial?",
                    required=True,
                ),
                "call_reference_number": text_ask(
                    "Call Reference Number",
                    "May I have a call reference number for this call?",
                    required=True,
                ),
            },
        ),
```

And `_patient_verification()` (line ~995), whose single leaf is the call-opening membership check:

```python
    return Section(
        title="Patient Verification",
        description=(
            "Outcome of the call-opening membership check. Recorded during the "
            "introduction task; a denial terminates the call via the "
            "insurance_not_active flow rule."
        ),
        # Whether the policy is active TODAY is a per-call fact, re-verified on every call — which
        # also re-arms the insurance_not_active flow rule on a retry, so a policy that lapsed since
        # the last attempt terminates properly instead of proceeding on a stale "Yes". Retaining
        # the introduction task (and with it the greeting and recording disclosure) falls out of
        # this; `test_a_task_carrying_the_calls_opening_is_always_retained` pins that dependency.
        collected_per="call",
        fields={
            "is_insurance_active": enum_ask(
                "Is Insurance Active",
                "Can you confirm the patient's insurance is currently active?",
                YES_NO,
            ),
        },
    )
```

- [ ] **Step 4: Mark the disease-only section and its activity leaf**

`packages/vera_core/src/vera_core/forms/catalog/disease_only.py:343` — same shape, same two
ask-role leaves:

```python
        "representative_details": Section(
            title="Insurance Representative",
            # See the IBV standard's insurance_representative section.
            collected_per="call",
            fields={
                "rep_name": text_ask(
                    "Representative Name",
                    "May I have your first name and last name initial?",
                    required=True,
                ),
                "call_reference_number": text_ask(
                    "Call Reference Number",
                    "May I have a call reference number for this call?",
                    required=True,
                ),
            },
        ),
```

Disease-only's opening task is `policy_basics`, whose two sections hold eleven collectable leaves —
plan name, effective date, group number, benefit year type, renewal date, annual maximum. Those are
form facts, so the section cannot be marked. Only `disease_coverage_active` is per-call, so this one
is a **leaf-level** mark (line ~193), which is what `enum_ask` needs the parameter for:

```python
            "disease_coverage_active": enum_ask(
                "Disease Coverage Active",
                "Is disease-specific coverage active on this policy?",
                YES_NO,
                # Per-call, like the IBV form's is_insurance_active: whether coverage is live TODAY
                # is re-verified every call. Marked on the LEAF, not the section — coverage_summary
                # also holds benefit_year_type, renewal_date and annual_benefit_maximum, which are
                # form facts and must not become per-call.
                collected_per="call",
            ),
```

and in `authoring.py:364`, `enum_ask` gains the pass-through — keyword-only, defaulting to `None`,
so every existing call site is unaffected:

```python
def enum_ask(
    title: str,
    ask_text: str,
    values: list[str],
    *,
    required: bool | RequiredWhen = True,
    default: str | None = None,
    applicable_when: Condition | None = None,
    hints: list[str] | None = None,
    collected_per: CollectedPer | None = None,
) -> Leaf:
    return Leaf(
        type="enum",
        title=title,
        role="ask",
        values=values,
        required=required,
        default=default,
        applicable_when=applicable_when,
        prompt=ask(ask_text, hints),
        collected_per=collected_per,
    )
```

`text_ask` does **not** need the parameter — both representative sections are marked at section
level and inherit. Thread it through other helpers only when a schema first needs it.

- [ ] **Step 5: Recompile the artifacts**

Run: `just compile-schemas`
Expected: exactly **two** added lines per schema file and nothing else —
`"collected_per": "call"` on `insurance_representative` and `patient_verification` for IBV, and on
`representative_details` and the `disease_coverage_active` leaf for disease-only.
`compile_document` uses `exclude_none=True, exclude_defaults=True`, so the other ~200 leaves emit
nothing. Verify with `git diff --stat data/form_schemas/` — `2 insertions` per file. More than that
means the field was declared in the wrong position or a section marker leaked. The freshness test in
`tests/unit/forms/test_schema_dsl.py` goes red on drift, so this step is not optional.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/forms/ -v`
Expected: PASS, including the freshness test and both new classes.

- [ ] **Step 7: Verify resolution on the real compiled artifacts**

Run:
```bash
uv run python -c "
import json, pathlib
from vera_core.forms.dsl import FormSchemaDoc
for f in sorted(pathlib.Path('data/form_schemas').glob('*.json')):
    if f.name == 'manifest.json':
        continue
    doc = FormSchemaDoc.model_validate(json.loads(f.read_text()))
    print(f.name)
    for p in sorted(doc.collected_per_call_paths()):
        print('   ', p)
"
```
Expected: exactly **three** paths per schema and nothing else —

```
ibv_form_standard_v2.json
     sections.insurance_representative.call_reference_number
     sections.insurance_representative.rep_name
     sections.patient_verification.is_insurance_active
disease_only_verification.json
     sections.coverage_summary.disease_coverage_active
     sections.representative_details.call_reference_number
     sections.representative_details.rep_name
```

Anything extra means a section marker leaked further than intended — most likely
`coverage_summary` marked at section level, which would wrongly make `benefit_year_type`,
`renewal_date` and `annual_benefit_maximum` per-call.

- [ ] **Step 8: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py \
        packages/vera_core/src/vera_core/forms/catalog/disease_only.py \
        data/form_schemas/ibv_form_standard_v2.json \
        data/form_schemas/disease_only_verification.json \
        tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): mark representative sections collected_per=call"
```

---

### Task 3: publish, re-seed and verify

**Files:**
- Modify: `scripts/seed_retry_form.py` — drop the `Call.call_reference_no` write

**Interfaces:**
- Consumes: the recompiled artifacts from Task 2.
- Produces: a published `schema_version` row per insurance type carrying the marker, and every
  `patient_form` re-seeded against it.

- [ ] **Step 1: Run the full gate**

Run: `just check`
Expected: PASS. Run it verbatim — `ruff check` and `ruff format --check` are different gates.

- [ ] **Step 2: Publish a new schema version, and re-seed every form onto it**

`just seed-schemas` demotes the currently published version to DRAFT and publishes a new one,
because the artifact changed. Forms pin `schema_version_id`, so an existing form keeps the OLD
unmarked document — and there is no migration to bring it forward (spec D7). Re-seed instead; the
only forms that exist are the two seeded test ones.

Run:
```bash
just up && just migrate && just seed-schemas
just test_seed_patient_data          # TEST-SEED-READY
just seed-retry-form                 # TEST-SEED-RETRY
```
Expected: the `seed-schemas` summary reports a new version per insurance type, not `(unchanged)`.

If a form you care about is pinned to the old version, delete it — do not add a migration. Confirm
none is left behind:
```bash
DB=$(uv run python -c "
from sqlalchemy.engine import make_url
from vera_core.config import get_settings
print(make_url(get_settings().database_url).database)")
docker exec vera-backend-postgres-1 psql -U vera -d "$DB" -c "
select pf.chart_number, sv.version, sv.status
from patient_form pf join schema_version sv on sv.id = pf.schema_version_id
where sv.status <> 'published'"
```
Expected: **0 rows**. Any row here is a form pinned to a demoted document, which Plan B's deletion
of `bookend_paths` would leave without a greeting — re-seed or delete it.

- [ ] **Step 3: Confirm the published document carries the marker**

Read it back out of the database rather than trusting the seed summary. `settings.database_url` is
a plain `str`, so take the database name through SQLAlchemy's `make_url` rather than attribute
access (each worktree has its own branch database):

```bash
DB=$(uv run python -c "
from sqlalchemy.engine import make_url
from vera_core.config import get_settings
print(make_url(get_settings().database_url).database)")
docker exec vera-backend-postgres-1 psql -U vera -d "$DB" -c "
select fs.insurance_type, sv.version, sv.status,
       sv.schema_json #>> '{sections,insurance_representative,collected_per}' as ibv_rep,
       sv.schema_json #>> '{sections,patient_verification,collected_per}'     as ibv_verify,
       sv.schema_json #>> '{sections,representative_details,collected_per}'   as dz_rep,
       sv.schema_json #>> '{sections,coverage_summary,fields,disease_coverage_active,collected_per}'
         as dz_active
from schema_version sv
join form_schema fs on fs.id = sv.schema_id
order by fs.insurance_type, sv.version"
```
Expected: the newest row per insurance type is `published` and shows `call` in the two columns
belonging to its insurance type, null in the other two. Older DRAFT rows will show nulls — that is
expected and fine, because Step 2 confirmed no form is pinned to them.

- [ ] **Step 4: Re-seed the retry scenario against the new version**

The seeded form pins whichever version was published when it was created, so it must be re-seeded
to bind to the marked one.

Run: `just seed-retry-form`
Expected: the printed `focus gate (reference captured) : True` and `still owed` / `after group
expansion` counts are unchanged from before this plan — Plan A adds vocabulary, not behaviour. A
change here means something read the marker prematurely.

- [ ] **Step 5: Drop the seed script's dead-column write**

`scripts/seed_retry_form.py` sets `Call.call_reference_no`, which implies that column is what makes
a call authoritative. It is not — nothing in the pipeline reads or writes it, and Plan B resolves
authority from `field_answer` (spec D8). Leaving it there would send the next reader down the wrong
path. Remove the `call_reference_no=_FIRST_CALL_REFERENCE` argument from the `Call(...)`
construction; the `field_answer` row at the reference path stays and is what actually matters.

Re-run `just seed-retry-form` afterwards and confirm `focus gate (reference captured) : True` is
unchanged — it reads the answer, not the column.

- [ ] **Step 6: Run `/simplify`, then re-run the gate**

Run `/simplify` on the change, then `just check`.
Expected: PASS on the exact tree to be pushed.

- [ ] **Step 7: Commit**

If `/simplify` produced edits, stage and commit them:

```bash
git add -u && git commit -m "refactor(forms): simplify collected_per resolution"
```

If it produced none, there is nothing to commit — Tasks 1-3 already committed the work.

---

## Verification

Plan A is done when:

- `uv run pytest tests/unit/forms/ -v` passes, including the artifact freshness test.
- `just check` passes verbatim on the pushed tree.
- Both compiled artifacts resolve exactly **three** call-scoped paths each (Task 2 Step 7), and
  `git diff --stat data/form_schemas/` shows 2 insertions per file and nothing else.
- The newest published `schema_version` per insurance type carries `collected_per: "call"` on both
  of its per-call nodes (Task 3 Step 3), and **no `patient_form` is pinned to a demoted version**
  (Task 3 Step 2). That second check is the precondition Plan B relies on to delete
  `bookend_paths`.
- The two tripwires pass: the opening task of any schema that declares an `intro` is retained by the
  derived retention rule, and so is the closing task.
- `just seed-retry-form` reports the same focus counts as before the change — Plan A is inert.

No eval-harness or live-call verification is needed: nothing reads the marker yet. Spoken
behaviour changes in Plan C, which is where the harness and a live browser-callee retry become
mandatory.
