# Plan B — authoritative focus set + reference-number gate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a retry's ask set be "everything no authoritative call confirmed" — and make a retry
happen at all from the operator surface.

**Architecture:** One new satisfaction predicate (`is_call_confirmed`) beside the existing one, one
new query for the authoritative-call set, one opt-in on `_required_paths` for defaulted leaves, and
one composed function (`focus_paths`) that replaces the dispatcher's ad-hoc three-way composition.
The `call_mode == RETRY` precondition on focus is dropped so the captured reference number is the
sole gate. `bookend_paths` is deleted.

**Tech Stack:** Python 3.12, pydantic v2, SQLAlchemy async, pytest, ruff, mypy `--strict`.

**Spec:** `vera-backend/docs/superpowers/specs/2026-08-21-retry-call-scoping-design.md`

## Global Constraints

- Every command runs from `vera-backend/`.
- **`is_field_satisfied` is NOT modified.** `completion_pct_v2`, `unsatisfied_required_paths` and
  `retryable_required_paths` keep today's behaviour. `verified_pct` and the park gate change in
  Plan D, not here (spec D3, D9).
- `vera_core/forms/` stays **pure and DB-free**. The authoritative-call query belongs in
  `vera_core/services/`, not in `forms/review.py`.
- **`Call.call_reference_no` is dead** — nothing in the pipeline writes it. Authoritative-ness is
  resolved from `field_answer` only (spec D8). Do not read that column.
- The authoritative-call query takes **no `is_current` filter**: attempt 1's reference row is
  superseded by attempt 2's, but attempt 1 was still authoritative.
- `load_field_status`' docstring promises the query is PHI-free. `call_id` is an id, so that holds —
  keep the promise and the comment true.
- Never log a field value. `focus_paths` operates on PHI.
- Depends on Plan A: `FormSchemaDoc.collected_per_call_paths()` must exist, both catalogs must be
  marked, and the re-seed must be done. **No migration, no pre-flight row check, and no runtime
  fallback** (spec D7) — this branch requires the schema version Plan A publishes. A pre-marker
  document is covered by Plan A's re-seed check; a schema authored without the marker is covered by
  Plan A's catalog tests, which fail CI. Neither needs a code path here.
- `just check` verbatim, then `/simplify`, then `just check` again, before claiming done.

---

## File Structure

- **Modify** `packages/vera_core/src/vera_core/forms/review.py`
  - `FieldStatus` gains `call_id` (Task 1)
  - `is_call_confirmed` (Task 2)
  - `_required_paths(..., include_defaulted=False)` (Task 3)
  - `focus_paths(...)` (Task 4)
- **Modify** `packages/vera_core/src/vera_core/services/field_status.py`
  - `load_field_status` selects `call_id` (Task 1)
  - `load_authoritative_call_ids` (Task 1)
- **Modify** `packages/vera_core/src/vera_core/services/queue_dispatcher.py:395-425` — the focus
  block (Task 5)
- **Modify** `packages/vera_core/src/vera_core/forms/call_plan.py` — delete `bookend_paths`
  (Task 5)
- **Modify** `tests/unit/forms/test_call_plan.py` — delete its `bookend_paths` tests (Task 5)
- **Test** `tests/unit/forms/test_focus_paths.py` — the new composition (Tasks 2-4)
- **Test** `tests/integration/test_authoritative_calls.py` — the query (Task 1)

**Interfaces produced, for Plans C, D and E:**

```python
# forms/review.py
@dataclass(frozen=True)
class FieldStatus:
    source: str | None
    ai_supported: bool | None
    ai_confidence: int | None
    call_id: UUID | None = None       # appended, so positional constructions still work

def is_call_confirmed(
    status: FieldStatus | None, *, authoritative_calls: Collection[UUID], floor: int
) -> bool: ...

def focus_paths(
    doc: FormSchemaDoc,
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any],
    authoritative_calls: Collection[UUID],
) -> list[str]: ...

# services/field_status.py
async def load_authoritative_call_ids(
    session: AsyncSession, form_id: UUID, *, reference_field: str
) -> frozenset[UUID]: ...
```

---

### Task 1: carry `call_id`, and resolve which calls are authoritative

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/review.py` (the `FieldStatus` dataclass, line 225)
- Modify: `packages/vera_core/src/vera_core/services/field_status.py`
- Test: `tests/integration/test_authoritative_calls.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `FieldStatus.call_id`, `load_authoritative_call_ids`.

- [ ] **Step 1: Write the failing integration test**

This one needs a database — it is a query. Follow the conventions of the existing integration tests
under `tests/integration/` for session/tenant fixtures; read one neighbouring test module first and
reuse its fixtures rather than inventing new ones.

```python
"""Which of a form's calls count as authoritative (they captured a reference number)."""

from vera_core.services.field_status import load_authoritative_call_ids

REF = "sections.insurance_representative.call_reference_number"


async def test_a_call_that_captured_a_reference_is_authoritative(session, form, make_call) -> None:
    call = await make_call(form)
    await make_answer(session, form, call, REF, "8842-QX-77", is_current=True)
    assert await load_authoritative_call_ids(session, form.id, reference_field=REF) == {call.id}


async def test_a_call_with_no_reference_answer_is_not(session, form, make_call) -> None:
    call = await make_call(form)
    await make_answer(session, form, call, "sections.deductibles.individual.total", "$3,000")
    assert await load_authoritative_call_ids(session, form.id, reference_field=REF) == frozenset()


async def test_a_superseded_reference_still_makes_ITS_call_authoritative(
    session, form, make_call
) -> None:
    """Attempt 2's reference supersedes attempt 1's, but attempt 1 was still authoritative — an
    `is_current` filter here would demote it and re-ask everything it collected."""
    first, second = await make_call(form), await make_call(form)
    await make_answer(session, form, first, REF, "R1", is_current=False)
    await make_answer(session, form, second, REF, "R2", is_current=True)
    assert await load_authoritative_call_ids(session, form.id, reference_field=REF) == {
        first.id,
        second.id,
    }


async def test_an_intake_answer_at_the_reference_path_makes_no_call_authoritative(
    session, form
) -> None:
    """An intake row has call_id NULL; authority comes from a CALL having captured it."""
    await make_answer(session, form, None, REF, "R-from-sheet", source="intake")
    assert await load_authoritative_call_ids(session, form.id, reference_field=REF) == frozenset()
```

`make_answer` is a helper to add beside these tests: it inserts one `FieldAnswer` with the given
`form_id`, `call_id`, `field_path`, `{"value": …}`, `source` (default `ai_call`) and `is_current`
(default `True`), then flushes.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `just up && just migrate && uv run pytest tests/integration/test_authoritative_calls.py -v`
Expected: FAIL on `ImportError: cannot import name 'load_authoritative_call_ids'`.

- [ ] **Step 3: Add `call_id` to `FieldStatus`**

`review.py`, the dataclass at line 225. **Appended last with a default**, so the many existing
three-argument constructions across `tests/unit/forms/` keep working:

```python
@dataclass(frozen=True)
class FieldStatus:
    """Immutable snapshot of a filled field's satisfaction state: source, AI confidence, and the
    call that produced it. An unfilled field has no status at all (absent from the map).

    `call_id` is NULL for intake/human answers. It is what lets `is_call_confirmed` ask whether an
    AUTHORITATIVE call produced this value — see spec D8."""

    source: str | None
    ai_supported: bool | None
    ai_confidence: int | None
    call_id: UUID | None = None
```

Add `from uuid import UUID` to `review.py`'s imports. It is stdlib, so the module stays DB-free.

- [ ] **Step 4: Select `call_id` in `load_field_status`**

`services/field_status.py`. Add `FieldAnswer.call_id` to the `select(...)` list and pass it through
into the constructed `FieldStatus`. The docstring already promises "no value or evidence columns, so
this query is PHI-free" — extend it rather than leaving it stale:

```python
    Selects only field_path, source, confidences, the latest eval's supported flag, and the
    originating call id — no value or evidence columns, so this query is PHI-free.
```

- [ ] **Step 5: Add `load_authoritative_call_ids`**

Same module:

```python
async def load_authoritative_call_ids(
    session: AsyncSession, form_id: UUID, *, reference_field: str
) -> frozenset[UUID]:
    """The form's calls that captured a rep call reference number.

    A call without one is not authoritative: nothing ties the conversation to a payer-side record,
    so the answers it collected carry no proof and a retry still owes those fields (spec D8).

    Deliberately NOT filtered on `is_current`: attempt 2's reference supersedes attempt 1's, but
    attempt 1 was still authoritative — filtering would demote every earlier authoritative call and
    re-ask everything it collected. Ids only, so this query is PHI-free.

    `Call.call_reference_no` is not consulted: the column exists but nothing in the pipeline writes
    it, which is why `has_call_reference` reads `field_answer` too.
    """
    rows = await session.execute(
        select(FieldAnswer.call_id).where(
            FieldAnswer.form_id == form_id,
            FieldAnswer.field_path == reference_field,
            FieldAnswer.call_id.is_not(None),
        )
    )
    return frozenset(call_id for call_id in rows.scalars() if call_id is not None)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_authoritative_calls.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/review.py \
        packages/vera_core/src/vera_core/services/field_status.py \
        tests/integration/test_authoritative_calls.py
git commit -m "feat(forms): resolve which of a form's calls are authoritative"
```

---

### Task 2: `is_call_confirmed`

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/review.py`
- Test: `tests/unit/forms/test_focus_paths.py`

**Interfaces:**
- Consumes: `FieldStatus.call_id` from Task 1.
- Produces: `is_call_confirmed`.

- [ ] **Step 1: Write the failing test**

```python
"""`is_call_confirmed` — did an AUTHORITATIVE call collect this, judge-supported?"""

from uuid import uuid4

from vera_core.forms.review import FieldStatus, is_call_confirmed, is_field_satisfied

AUTH, OTHER = uuid4(), uuid4()
CALLS = frozenset({AUTH})


def _status(source: str, *, supported=True, confidence=95, call_id=AUTH) -> FieldStatus:
    return FieldStatus(
        source=source, ai_supported=supported, ai_confidence=confidence, call_id=call_id
    )


class TestIsCallConfirmed:
    def test_authoritative_call_supported_answer_is_confirmed(self) -> None:
        assert is_call_confirmed(_status("ai_call"), authoritative_calls=CALLS, floor=70)

    def test_answer_from_a_non_authoritative_call_is_not(self) -> None:
        """The rep answered, but nothing ties the conversation to a payer record."""
        assert not is_call_confirmed(
            _status("ai_call", call_id=OTHER), authoritative_calls=CALLS, floor=70
        )

    def test_intake_value_is_not_confirmed_even_though_it_is_satisfied(self) -> None:
        """The divergence from `is_field_satisfied` that this whole predicate exists for."""
        intake = FieldStatus(source="intake", ai_supported=None, ai_confidence=None, call_id=None)
        assert is_field_satisfied(intake, floor=70) is True
        assert is_call_confirmed(intake, authoritative_calls=CALLS, floor=70) is False

    def test_human_value_is_not_confirmed(self) -> None:
        human = FieldStatus(source="human", ai_supported=None, ai_confidence=None, call_id=None)
        assert not is_call_confirmed(human, authoritative_calls=CALLS, floor=70)

    def test_judge_rejected_answer_is_not_confirmed(self) -> None:
        assert not is_call_confirmed(
            _status("ai_call", supported=False, confidence=38), authoritative_calls=CALLS, floor=70
        )

    def test_below_floor_is_not_confirmed(self) -> None:
        assert not is_call_confirmed(
            _status("ai_call", confidence=69), authoritative_calls=CALLS, floor=70
        )

    def test_unjudged_answer_is_not_confirmed(self) -> None:
        """No `field_evaluation` row yet — `ai_supported` is None, so nothing is proven."""
        assert not is_call_confirmed(
            _status("ai_call", supported=None), authoritative_calls=CALLS, floor=70
        )

    def test_absent_status_is_not_confirmed(self) -> None:
        assert not is_call_confirmed(None, authoritative_calls=CALLS, floor=70)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/forms/test_focus_paths.py -k IsCallConfirmed -v`
Expected: FAIL on `ImportError: cannot import name 'is_call_confirmed'`.

- [ ] **Step 3: Implement it**

In `review.py`, directly below `is_field_satisfied` so the contrast is visible in one screen:

```python
def is_call_confirmed(
    status: FieldStatus | None, *, authoritative_calls: Collection[UUID], floor: int
) -> bool:
    """True only when an AUTHORITATIVE call collected this value and the judge supported it.

    The retry ask set's rule, and deliberately stricter than `is_field_satisfied`: an intake or
    human value is trusted for completeness and for the retry-WORTHINESS decision, but it was never
    put to the payer's representative, so a genuine retry still owes it. Answers from a call that
    captured no reference number are not proof either — see spec D8.

    This is `gating_seed`'s rule (an ask-role value on file is a pre-call baseline, never an answer)
    applied to the focus set, which is computed from `field_answer` and had no equivalent guard.
    """
    if status is None or status.source != AnswerSource.AI_CALL.value:
        return False
    if status.call_id is None or status.call_id not in authoritative_calls:
        return False
    return bool(status.ai_supported) and (status.ai_confidence or 0) >= floor
```

`Collection` is already imported from `collections.abc` in `review.py`; `UUID` was added in Task 1.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/forms/test_focus_paths.py -k IsCallConfirmed -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/review.py tests/unit/forms/test_focus_paths.py
git commit -m "feat(forms): is_call_confirmed — authoritative-call satisfaction"
```

---

### Task 3: let the ask set include defaulted leaves

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/review.py` (`_required_paths`, line 248)
- Test: `tests/unit/forms/test_focus_paths.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_required_paths(..., include_defaulted: bool = False)`.

A leaf declaring `default` is excluded today because completion counts it filled. `owed_now` — the
fresh-call predicate behind the `task_complete` refusal and the gap pass — takes the opposite view:
*"`default` is deliberately not consulted: it declares the value a field takes when not collected,
never that the question need not be asked."* The retry ask set is the last place still carrying the
pre-fix behaviour, so it opts in. Everything else keeps the exclusion.

- [ ] **Step 1: Write the failing test**

```python
class TestDefaultedLeavesInTheAskSet:
    """The retry ask set follows `owed_now`, not `completion_pct_v2`, on defaulted leaves."""

    def test_retryable_required_paths_still_excludes_them(self) -> None:
        """Unchanged: this predicate answers "is a retry WORTH placing", and a defaulted leaf the
        form calls done must not keep a form redialing (spec D3)."""
        doc, raw = _ibv()
        owed = retryable_required_paths(_nothing_answered(raw), raw, floor=70, values=_family())
        assert "sections.patient_information.spouse_partner_name" not in owed

    def test_the_ask_set_includes_them(self) -> None:
        doc, raw = _ibv()
        asked = _required_paths_for_asking(raw, _family())
        assert "sections.patient_information.spouse_partner_name" in asked
        assert "sections.patient_information.spouse_partner_dob" in asked

    def test_the_seven_family_plan_defaulted_leaves(self) -> None:
        """Measured on ibv_form_standard_v2: 40 askable required+applicable today, 47 with
        defaults. These seven are the ones a fresh call asks and a retry silently skips."""
        doc, raw = _ibv()
        today = set(_required_paths_for_asking(raw, _family(), include_defaulted=False))
        with_defaults = set(_required_paths_for_asking(raw, _family(), include_defaulted=True))
        assert with_defaults - today == {
            "sections.patient_information.spouse_partner_name",
            "sections.patient_information.spouse_partner_dob",
            "sections.insurance_information.group_name",
            "sections.insurance_information.group_number",
            "sections.insurance_information.policy_situs",
            "sections.benefit_coverage.telehealth_covered",
            "sections.enrollment.enrollment_required",
        }
```

Helpers to add beside them: `_ibv()` loads `data/form_schemas/ibv_form_standard_v2.json` and returns
`(FormSchemaDoc, raw_dict)`; `_family()` returns
`{"sections.benefit_coverage.coverage_type": "Family"}`; `_nothing_answered(raw)` returns an empty
status map; `_required_paths_for_asking(raw, values, include_defaulted=True)` calls the private
`_required_paths(raw, values, askable_only=True, include_defaulted=include_defaulted)`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/forms/test_focus_paths.py -k DefaultedLeaves -v`
Expected: FAIL — `_required_paths()` got an unexpected keyword argument `include_defaulted`.

- [ ] **Step 3: Add the parameter**

```python
def _required_paths(
    schema_json: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    askable_only: bool,
    include_defaulted: bool = False,
) -> list[str]:
    """Paths of required, applicable leaves that could still owe an answer — optionally only
    collectible (ask/confirm) ones. v2: filters by role + applicability. v1: returns all required
    paths (no role concept).

    A leaf declaring a `default` is excluded by default: `completion_pct_v2` counts it filled and
    the export writes it, so leaving it here would block auto-completion on a field the form calls
    done. `include_defaulted=True` is the RETRY ASK SET only, which follows `owed_now` — a default
    declares the value a field takes when not collected, never that the question need not be asked.
    """
    if is_v2(schema_json):
        doc = FormSchemaDoc.model_validate(schema_json)
        shared = doc.shared_conditions or {}
        return [
            path
            for path, leaf, gates in leaf_gates(doc)
            if (not askable_only or leaf.role in COLLECTED_ROLES)
            and (include_defaulted or leaf.default is None)
            and is_applicable(gates, values, shared)
            and is_required(leaf, values, shared)
        ]
    return all_required_paths(schema_json)
```

Leave `retryable_required_paths`, `unsatisfied_required_paths`, `satisfied_required_fraction` and
`_unsatisfied` untouched — they all take the default `include_defaulted=False`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/forms/ -v`
Expected: PASS, including every pre-existing test in `test_review.py` and
`test_retryable_fields.py` — the parameter's default preserves their behaviour exactly.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/review.py tests/unit/forms/test_focus_paths.py
git commit -m "feat(forms): let the retry ask set include defaulted leaves"
```

---

### Task 4: `focus_paths`

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/review.py`
- Test: `tests/unit/forms/test_focus_paths.py`

**Interfaces:**
- Consumes: `is_call_confirmed` (Task 2), `_required_paths(include_defaulted=…)` (Task 3),
  `FormSchemaDoc.collected_per_call_paths()` (Plan A), and the existing `expand_to_groups`.
- Produces: `focus_paths(...) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
class TestFocusPaths:
    def test_an_authoritatively_confirmed_field_is_not_asked_again(self) -> None:
        doc, raw = _ibv()
        target = "sections.insurance_information.plan_type"
        status = {target: FieldStatus("ai_call", True, 95, AUTH)}
        paths = focus_paths(doc, status, raw, floor=70, values={target: "PPO"},
                            authoritative_calls={AUTH})
        assert target not in paths

    def test_the_same_field_from_a_non_authoritative_call_IS_asked_again(self) -> None:
        doc, raw = _ibv()
        target = "sections.insurance_information.plan_type"
        status = {target: FieldStatus("ai_call", True, 95, OTHER)}
        paths = focus_paths(doc, status, raw, floor=70, values={target: "PPO"},
                            authoritative_calls={AUTH})
        assert target in paths

    def test_an_intake_value_is_asked_because_no_call_confirmed_it(self) -> None:
        doc, raw = _ibv()
        target = "sections.insurance_information.group_name"
        status = {target: FieldStatus("intake", None, None, None)}
        paths = focus_paths(doc, status, raw, floor=70, values={target: "Umbrella"},
                            authoritative_calls={AUTH})
        assert target in paths

    def test_call_scoped_paths_are_always_present(self) -> None:
        """Even fully confirmed by an authoritative call: they describe THIS call (Plan A)."""
        doc, raw = _ibv()
        ref = doc.rep_call_reference_number_field
        status = {p: FieldStatus("ai_call", True, 95, AUTH) for p in doc.collected_per_call_paths()}
        paths = focus_paths(doc, status, raw, floor=70,
                            values=dict.fromkeys(status, "x"), authoritative_calls={AUTH})
        assert doc.collected_per_call_paths() <= set(paths)
        assert ref in paths

    def test_one_missing_group_member_pulls_in_its_whole_panel(self) -> None:
        """`expand_to_groups`: a partial panel reads oddly on a call."""
        doc, raw = _ibv()
        target = "sections.diagnostic_testing.labs_xray_ultrasound.cpt_58340.copay"
        status = {target: FieldStatus("ai_call", False, 38, AUTH)}
        paths = set(focus_paths(doc, status, raw, floor=70, values={target: "$25"},
                                authoritative_calls={AUTH}))
        panel = "sections.diagnostic_testing.labs_xray_ultrasound."
        assert len([p for p in paths if p.startswith(panel)]) == 32

    def test_returns_document_order_without_duplicates(self) -> None:
        doc, raw = _ibv()
        paths = focus_paths(doc, {}, raw, floor=70, values={}, authoritative_calls=set())
        assert len(paths) == len(set(paths))
        order = doc.collection_paths()
        ranked = [order.index(p) for p in paths if p in order]
        assert ranked == sorted(ranked)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/forms/test_focus_paths.py -k FocusPaths -v`
Expected: FAIL on `ImportError: cannot import name 'focus_paths'`.

- [ ] **Step 3: Implement it**

```python
def focus_paths(
    doc: FormSchemaDoc,
    status_by_path: Mapping[str, FieldStatus],
    schema_json: Mapping[str, Any],
    *,
    floor: int,
    values: Mapping[str, Any],
    authoritative_calls: Collection[UUID],
) -> list[str]:
    """Every path a FOCUSED retry should put to the representative, in document order.

    Three sources, unioned:

    * required, applicable, askable leaves no AUTHORITATIVE call confirmed (`is_call_confirmed`) —
      which covers never-collected, judge-rejected, intake-supplied-but-never-confirmed, and
      collected-by-an-unverifiable-call alike;
    * every collectable leaf of a group any of those falls inside (`expand_to_groups`) — a partly
      re-asked panel reads oddly on a call;
    * every `collected_per="call"` leaf, whatever is on file — the rep's name and the call reference
      number describe THIS call, and keeping them is also what retains the greeting and wrap-up
      tasks, since `focus_call_plan` drops a task with no kept fields.

    Defaulted leaves are included: this is the ask set, and a `default` declares the value a field
    takes when not collected, never that the question need not be asked (`owed_now`).

    NOT the retry-worthiness decision — `retryable_required_paths` still answers that, and must
    keep excluding call-scoped and defaulted leaves or a form whose only gaps are unaskable would
    redial to no benefit (spec D3). Values are PHI; never log them.
    """
    applicable = _required_paths(
        schema_json, values, askable_only=True, include_defaulted=True
    )
    alternatives = _alternatives(schema_json)
    owed = [
        path
        for path in applicable
        if not _confirmed(path, status_by_path, alternatives, authoritative_calls, floor=floor)
    ]
    wanted = set(expand_to_groups(doc, owed)) | doc.collected_per_call_paths()
    ordered = [path for path in doc.collection_paths() if path in wanted]
    ordered.extend(sorted(wanted.difference(ordered)))
    return ordered


def _confirmed(
    path: str,
    status_by_path: Mapping[str, FieldStatus],
    alternatives: AlternativeIndex,
    authoritative_calls: Collection[UUID],
    *,
    floor: int,
) -> bool:
    """Confirmed itself, or by a sibling in its either/or group — one answer satisfies the pair.
    Mirrors `_satisfied`, swapping in the authoritative-call rule."""
    if is_call_confirmed(status_by_path.get(path), authoritative_calls=authoritative_calls, floor=floor):
        return True
    return any(
        is_call_confirmed(
            status_by_path.get(other), authoritative_calls=authoritative_calls, floor=floor
        )
        for other in alternatives.get(path, ())
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/forms/test_focus_paths.py -v`
Expected: PASS.

- [ ] **Step 5: Verify the acceptance numbers, and the boundary with Plan C**

Run:
```bash
uv run python -c "
import json, pathlib
from uuid import uuid4
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.review import FieldStatus, focus_paths
raw = json.loads(pathlib.Path('data/form_schemas/ibv_form_standard_v2.json').read_text())
doc = FormSchemaDoc.model_validate(raw)
auth = uuid4()

# (a) intake-supplied askable leaves are now owed
target = 'sections.insurance_information.group_name'
st = {target: FieldStatus('intake', None, None, None)}
print('intake value owed        :', target in focus_paths(
    doc, st, raw, floor=70, values={target: 'Umbrella'}, authoritative_calls={auth}))

# (b) a non-authoritative call's answer is owed
other = uuid4()
st = {target: FieldStatus('ai_call', True, 95, other)}
print('non-authoritative owed   :', target in focus_paths(
    doc, st, raw, floor=70, values={target: 'Umbrella'}, authoritative_calls={auth}))

# (c) the gate-parent case: still ONE path. Plan C's explode recovers the dependents.
parent = 'sections.infertility_treatment.infertility_tx_covered'
st = {p: FieldStatus('ai_call', True, 95, auth) for p, _ in doc.leaf_items() if p != parent}
vals = {p: 'Yes' for p in st}
paths = focus_paths(doc, st, raw, floor=70, values=vals, authoritative_calls={auth})
infert = [p for p in paths if p.startswith('sections.infertility_treatment.')]
print('infertility paths        :', len(infert), '(expected 1 — see below)')
"
```
Expected: `(a)` and `(b)` are both `True` — the two cases spec D8 exists for. `(c)` is **1**, and
that is correct for this plan: `_required_paths` still filters on `is_applicable`, so an unanswered
gate parent still hides its dependents. Recovering those 72 is `focus_questions(explode=True)` in
Plan C, not here. Do not "fix" it in B by loosening the applicability filter — that would put
inapplicable questions in the ask set on a fresh call too.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/review.py tests/unit/forms/test_focus_paths.py
git commit -m "feat(forms): focus_paths — the authoritative retry ask set"
```

---

### Task 5: wire the dispatcher, and delete `bookend_paths`

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/queue_dispatcher.py:395-425`
- Modify: `packages/vera_core/src/vera_core/forms/call_plan.py` — delete `bookend_paths`
- Modify: `tests/unit/forms/test_call_plan.py` — delete its `bookend_paths` tests
- Test: `tests/integration/test_retry_dispatch.py`

**Interfaces:**
- Consumes: `focus_paths` (Task 4), `load_authoritative_call_ids` (Task 1).
- Produces: a dispatched retry whose staged plan is narrowed to `focus_paths`.

- [ ] **Step 1: Write the failing integration test**

```python
"""A retry dispatches focused, gated on the captured reference number rather than on retry_count."""


async def test_a_form_with_a_captured_reference_dispatches_focused(...) -> None:
    """The defect this plan fixes: the operator surface resets retry_count, so `call_mode` was
    FULL and the focus branch never ran (spec, "The observed call was never a retry")."""
    form = await seed_form_with_authoritative_call(...)   # retry_count deliberately 0
    await try_dispatch(session, tenant_id, livekit, kms, audit, plan_service=plans)
    staged = await plans.get(room_name_for_call(tenant_id, (await only_call(form)).id))
    assert {f.path for t in staged.tasks for f in t.fields} < ALL_PATHS   # narrowed
    assert form.retry_count == 0                                          # gate is not the counter


async def test_a_form_with_no_captured_reference_dispatches_the_full_plan(...) -> None:
    form = await seed_form_without_reference(...)
    await try_dispatch(...)
    staged = await plans.get(...)
    assert {f.path for t in staged.tasks for f in t.fields} == ALL_PATHS


async def test_the_greeting_and_wrap_up_tasks_survive_the_narrowing(...) -> None:
    """Without `bookend_paths`, this holds only because their leaves are `collected_per="call"`."""
    form = await seed_form_with_authoritative_call(...)
    await try_dispatch(...)
    staged = await plans.get(...)
    keys = {t.task_key for t in staged.tasks}
    assert {"introduction", "wrap_up"} <= keys
```

Read `tests/integration/` for the existing dispatcher-test fixtures (a fake livekit gateway, a
`CallPlanService` over a real or fake store) and reuse them; `test_post_call_eval.py` is the closest
neighbour for form/call seeding shape.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_retry_dispatch.py -v`
Expected: FAIL — the staged plan is the full plan, because `call_mode` is FULL.

- [ ] **Step 3: Replace the dispatcher's focus block**

`queue_dispatcher.py`. Replace the whole `if call_mode == CallMode.RETRY and staged_plan is not None:`
block (through the `focus_call_plan` assignment) with:

```python
        # Retry scope: with a call reference number captured, this is a FOCUSED retry — stage a
        # plan narrowed to what no authoritative call has confirmed, so the agent asks ONLY those
        # and never announces a prior call. Without a reference number it runs FRESH.
        #
        # Gated on the captured reference number, NOT on `call_mode`: the operator surface passes
        # `manual=True`, which resets `retry_count`, so a form with 152 confirmed answers and a
        # reference on file dispatched as FULL and re-asked everything (spec D4).
        if staged_plan is not None:
            version = schema_versions.get(form.schema_version_id)
            if version is None:
                version = (
                    await session.execute(
                        select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
                    )
                ).scalar_one()
                schema_versions[form.schema_version_id] = version
            doc = FormSchemaDoc.model_validate(version.schema_json)
            status_by_path = await load_field_status(session, form.id)
            if has_call_reference(status_by_path, doc):
                plan, plan_prompt_version_id = staged_plan
                authoritative = await load_authoritative_call_ids(
                    session, form.id, reference_field=doc.rep_call_reference_number_field
                )
                focus = focus_paths(
                    doc,
                    status_by_path,
                    version.schema_json,
                    floor=retry_floor,
                    values=values,
                    authoritative_calls=authoritative,
                )
                if focus:
                    staged_plan = (focus_call_plan(plan, focus), plan_prompt_version_id)
```

Update the imports: drop `bookend_paths`, `expand_to_groups` and `retryable_required_paths` from the
`forms.call_plan` / `forms.review` import lists if nothing else in the module uses them, and add
`focus_paths` and `load_authoritative_call_ids`. Let ruff tell you which became unused.

`call_mode` is still computed and still stamped on the `Call` row — it drives reporting and the
`CallLineage` branch below. Only the focus decision stops reading it.

- [ ] **Step 4: Delete `bookend_paths` and its tests**

Remove the function from `forms/call_plan.py` and the `test_includes_opening_and_wrapup_fields` /
`introduction`-and-`wrap_up`-key tests from `tests/unit/forms/test_call_plan.py`. Their intent
survives as Task 5 Step 2's third test and as Plan A's intro tripwire — both of which assert the
*outcome* (those tasks survive) rather than the positional mechanism.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_retry_dispatch.py tests/unit/forms/ -v`
Expected: PASS.

- [ ] **Step 6: Full gate, simplify, gate again**

Run: `just check`, then `/simplify`, then `just check`.
Expected: PASS on the exact tree to be pushed.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/services/queue_dispatcher.py \
        packages/vera_core/src/vera_core/forms/call_plan.py \
        tests/unit/forms/test_call_plan.py \
        tests/integration/test_retry_dispatch.py
git commit -m "feat(dispatch): focus a retry on the captured reference number"
```

---

## Verification

Plan B is done when:

- `just check` passes verbatim on the pushed tree.
- No `patient_form` is pinned to a pre-marker document (Plan A's check) — that, plus Plan A's
  catalog tests, is what makes deleting `bookend_paths` safe with no migration and no fallback.
- `bookend_paths` no longer exists anywhere: `grep -rn bookend_paths packages apps tests` is empty.
- On the seeded form, `just seed-retry-form` reports a focus set that now includes the defaulted
  leaves and, in the gate-parent variant, the previously dropped dependents.
- `is_field_satisfied`, `completion_pct_v2` and `unsatisfied_required_paths` are untouched —
  `git diff` shows no change to them.

**Do not stop here.** B narrows which fields the plan *tracks*; the agent still speaks every
question in the surviving tasks, because `focus_call_plan` does not touch `panels`. That is Plan C.
Shipping B alone widens today's P7 defect to more calls rather than fixing it, so a live call after
B and before C will still sound wrong — expected, not a regression.
