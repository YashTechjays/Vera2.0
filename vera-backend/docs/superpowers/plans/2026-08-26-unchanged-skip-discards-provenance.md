# Observer unchanged-skip provenance fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Observer record an answer the rep states even when it matches a value already on file from a *different* call (or from intake), so a confirmation creates provenance instead of being discarded.

**Architecture:** One statement changes. `ObserverManager._record_locked` currently dedups against `_on_file`, which is seeded from `plan.prefilled` and therefore mixes intake values, prior-call values, and this call's own writes. It will dedup against `_recorded`, which is already defined as "what THIS CALL collected". `_on_file` is left completely alone — it still serves the rule engine, the triplet derivation, and the canonical on-file map. Nothing outside the agent worker changes.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio (`asyncio_mode = "auto"`), mypy --strict, ruff. uv workspace: `vera_core` → `agent_worker` / `control_plane`.

**Spec:** `vera-backend/docs/superpowers/specs/2026-08-26-unchanged-skip-discards-provenance-design.md`

## Global Constraints

- All commands run from `vera-backend/`. Never `cd` to the original repo root — this is a worktree.
- **Run `just check` verbatim**, never a hand-picked subset. `ruff check` (lint) and `ruff format --check` (formatting) are DIFFERENT gates.
- **`mypy --strict` covers `tests/` too.** Every test helper needs full annotations.
- **A test must be able to fail. Prove it.** After writing a test, delete or invert the behaviour, confirm it FAILS, restore it. Record the mutation and the observed failure in the ledger. This branch shipped eight tests that passed with their feature deleted; every one was found by mutation and none by reading.
- **PHI:** answer values are PHI. Never log a value, never add one to a span. The existing `vera.observer.answer_recorded` span records path, confidence and task key only — keep it that way.
- Two pytest roots: `testpaths = ["tests", "apps/agent_worker/tests"]`. Observer tests live in `apps/agent_worker/tests/unit/test_observer.py`, NOT under `tests/`.
- Baseline to hold: `just check` → **2708 passed, 0 failed, 0 errors** (commit `11213bec`). The branch no longer carries a residue allowance; any red is real.
- Commit messages: **no `Co-Authored-By`** line.
- After implementation and before claiming done, run the `/simplify` skill on the change, then re-run `just check`.

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `apps/agent_worker/src/agent_worker/observer.py` | The Observer runtime. `_record_locked` owns the write/emit/dedup decision. | **Modify** — lines 556–565 only. |
| `apps/agent_worker/tests/unit/test_observer.py` | Observer runtime tests. | **Modify** — re-point 1 test, invert 1 test, add 3. |
| `tests/unit/services/test_field_answers.py` | `record_answer`'s supersede + idempotency logic against a fake session. | **Modify** — add 2 tests. |
| `tests/unit/forms/test_review.py` | `dispute_view` / `build_field_views` payload shape. | **Modify** — add 2 tests. |
| `tests/unit/forms/test_retryable_fields.py` | `is_field_satisfied` and the required-path gates. | **Modify** — add 1 assertion to an existing test. |
| `.superpowers/sdd/2026-08-25-retry-decision-backend/progress.md` | The ledger (git-ignored). | **Append** — mutation evidence + live-gate result. |

**Already covered — do NOT add duplicates.** The review-level half of the confirm-role cure is
tested today: `test_intake_does_not_satisfy_a_confirm_leaf` and `test_a_call_satisfies_a_confirm_leaf`
(`tests/unit/forms/test_retryable_fields.py:175,184`) already prove that an intake value leaves
`policy_number` unsatisfied and an `ai_call` value satisfies it. The spec's §5.2 item 2(b) is
therefore satisfied by existing coverage; only the Observer half (Task 2) is new. Cite those two
tests in the commit body rather than re-asserting them.

---

## Task 1: Re-point the snapping test before it can go vacuous

**Why first:** this test currently proves that `_on_file` is snapped by observing a *dedup*
side-effect (`run_state.records == []`). Task 2 removes that side-effect, at which point the test
can no longer fail if snapping breaks — it silently becomes vacuous. Re-pointing it at the rule
engine FIRST means it passes both before and after Task 2, and the suite is never red.

**Files:**
- Modify: `apps/agent_worker/tests/unit/test_observer.py:257-270`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. Test-only.

**Background the implementer needs:**
`ObserverManager.__init__` seeds `self._on_file` by running each `plan.prefilled` value through
`canonical_answer` (`observer.py:407-410`), which snaps a value onto its leaf's authored literal
spelling — so `" no limit "` becomes `"No Limit"` when the leaf declares
`special_values=["$0", "Unlimited", "No Limit"]`. The rule engine reads `_on_file` directly
(`observer.py:620`) and its `eq` comparison is a **raw string compare** with no normalization
(`conditions.py:55-58`, `_as_text` only stringifies). So a terminate rule keyed on the authored
literal fires only if the seed was snapped. That makes the rule engine a real observer of the
snapping, unlike the dedup.

The rule engine runs inside `_record_locked` **after** a successful write, so the test needs one
recorded answer to trigger evaluation. Use a *different* field as the trigger, so the rule fires off
the seeded prefill rather than off anything this call recorded.

- [ ] **Step 1: Replace the test**

Replace the whole of `test_a_prefill_is_snapped_before_it_seeds_the_gate_baseline`
(`apps/agent_worker/tests/unit/test_observer.py:257-270`) with:

```python
    @pytest.mark.asyncio
    async def test_a_prefill_is_snapped_before_it_seeds_the_rule_engine(self) -> None:
        """`_on_file` is what the rule engine compares byte-exact, and it is seeded from
        `plan.prefilled` — a prefill written before the writers canonicalized carries whatever
        spelling its source used. Asserted through the rule engine, the consumer that actually
        depends on the snapping: `conditions.evaluate`'s `eq` is a raw string compare, so a
        terminate rule keyed on the authored literal fires ONLY if the seed was snapped.
        Deliberately not asserted through the dedup — that stopped reading `_on_file`."""
        total = _field(
            "sections.a.total", type="currency", special_values=["$0", "Unlimited", "No Limit"]
        )
        stop = FlowRule(
            rule_key="stop",
            when=Comparison(field="sections.a.total", op="eq", value="No Limit"),
            action="terminate_call",
        )
        # The trigger is a DIFFERENT field, so the rule fires off the seeded prefill rather
        # than off anything this call recorded.
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, _, _, controller = _manager(
            _plan(
                fields=[total, _field("sections.a.x")],
                flow_rules=[stop],
                prefilled={"sections.a.total": " no limit "},
            ),
            extractor,
        )
        await _feed(manager, _rep("Yes, and there is no limit on that one."))
        assert controller.applied == [Terminate(rule_key="stop")]
```

`FlowRule`, `Comparison`, `Terminate` and `_field` are already imported in this file — no import changes.

- [ ] **Step 2: Run it and confirm it PASSES against unchanged production code**

```bash
uv run pytest "apps/agent_worker/tests/unit/test_observer.py::TestRecording::test_a_prefill_is_snapped_before_it_seeds_the_rule_engine" -v
```

Expected: PASS. (Production code is untouched so far; the snapping already works.)

- [ ] **Step 3: Mutation-prove it — this is the whole point of the task**

In `apps/agent_worker/src/agent_worker/observer.py:407-410`, temporarily drop the snapping:

```python
        self._on_file: dict[str, Any] = dict(plan.prefilled)
```

Re-run the command from Step 2.
Expected: **FAIL** — `assert [] == [Terminate(rule_key='stop')]`, because `_on_file` holds
`" no limit "` and the raw `eq` against `"No Limit"` is false.

Then **restore** `observer.py:407-410` exactly as it was and re-run: PASS.

Record in the ledger: the mutation applied, and the observed failure message.

- [ ] **Step 4: Run the whole observer suite**

```bash
uv run pytest apps/agent_worker/tests/unit/test_observer.py
```

Expected: all pass (the count is whatever it was before — this task adds no tests).

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/tests/unit/test_observer.py
git commit -m "test(observer): prove prefill snapping through the rule engine, not the dedup

The dedup side-effect this test asserted on is about to stop reading _on_file,
which would leave the test unable to fail if snapping broke. Re-pointed at the
rule engine, the consumer that genuinely depends on the snapped seed:
conditions.evaluate's eq is a raw string compare, so a terminate rule keyed on
the authored literal fires only when the seed was snapped.

Mutation-proved: replacing the canonical_answer seed with dict(plan.prefilled)
fails it with [] != [Terminate(rule_key='stop')]."
```

---

## Task 2: Swap the dedup key, and invert the test that asserts the defect

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/observer.py:556-565`
- Modify: `apps/agent_worker/tests/unit/test_observer.py:242-255`
- Test: `apps/agent_worker/tests/unit/test_observer.py` (3 new tests)

**Interfaces:**
- Consumes: nothing.
- Produces: no new symbols. Behaviour change only — `_record_locked` writes in strictly more cases.

**Background the implementer needs:**
`self._recorded` (`observer.py:413`) is "What THIS CALL collected". It is written only by
`_push_recorded`, which has exactly two call sites: `observer.py:564` (inside the skip branch being
deleted) and `observer.py:588` (after a completed write). Once the skip branch is gone, the only
remaining site runs post-write, so `_recorded[p] == v` implies this call wrote `v`. That is why the
controller still learns every value, and why the change can only ADD writes, never remove one.

- [ ] **Step 1: Write the three failing tests**

Add all three to `class TestRecording` in `apps/agent_worker/tests/unit/test_observer.py`,
immediately after `test_confirming_an_ask_role_prefill_still_reaches_the_controller`:

```python
    @pytest.mark.asyncio
    async def test_repeating_a_prior_calls_value_is_recorded_under_this_call(self) -> None:
        """The filed defect. `plan.prefilled` carries PRIOR-CALL values on a retry (it is built
        from `current_values_by_path`, every source), so the old `_on_file` dedup discarded the
        rep confirming one — leaving the previous, possibly non-authoritative, call owning the
        row forever. A confirmation is the strongest evidence there is; it must be written."""
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Individual", 90)])
        manager, run_state, bus, controller = _manager(
            _plan(prefilled={"sections.a.x": "Individual"}), extractor
        )
        await _feed(manager, _rep("Individual coverage, yes."))
        assert run_state.records == [(ROOM, "sections.a.x", "Individual", 0)]
        assert len(bus.events) == 1 and bus.events[0].field_path == "sections.a.x"
        assert controller.answers["sections.a.x"] == "Individual"

    @pytest.mark.asyncio
    async def test_confirming_a_confirm_role_prefill_is_recorded(self) -> None:
        """`policy_number` is `role="confirm", required=True`, and `_satisfied` refuses to count
        an intake value for a confirm leaf (proven by
        tests/unit/forms/test_retryable_fields.py::test_intake_does_not_satisfy_a_confirm_leaf).
        So under the old dedup, a rep confirming the read-back member ID wrote no row and the
        field stayed unsatisfied forever — the field whose entire purpose is payer confirmation
        could not be satisfied by a successful confirmation."""
        member_id = _field("sections.a.member_id", role="confirm")
        extractor = FakeExtractor([ExtractedAnswer("sections.a.member_id", "XYZ123", 90)])
        manager, run_state, bus, _ = _manager(
            _plan(fields=[member_id], prefilled={"sections.a.member_id": "XYZ123"}), extractor
        )
        await _feed(manager, _rep("Yes, XYZ123 is correct."))
        assert run_state.records == [(ROOM, "sections.a.member_id", "XYZ123", 0)]
        assert len(bus.events) == 1

    @pytest.mark.asyncio
    async def test_a_repeated_prefill_still_writes_only_one_row_per_call(self) -> None:
        """Row-volume bound. The old dedup bounded writes at one row per distinct value per
        call via `_on_file`; keying on `_recorded` must reproduce that bound exactly, or every
        re-extraction of the same confirmation writes another row."""
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Family", 90)])
        manager, run_state, bus, _ = _manager(
            _plan(prefilled={"sections.a.x": "Family"}), extractor
        )
        await _feed(manager, _rep("Family."))
        await _feed(manager, _rep("Still family."))
        await _feed(manager, _rep("Yes, family."))
        assert len(run_state.records) == 1
        assert len(bus.events) == 1
```

- [ ] **Step 2: Run them and confirm they FAIL**

```bash
uv run pytest apps/agent_worker/tests/unit/test_observer.py -k "repeating_a_prior_calls_value or confirming_a_confirm_role_prefill or repeated_prefill_still_writes" -v
```

Expected: the first two FAIL with `assert [] == [(...)]` (the skip discarded the write). The third
PASSES already (one record is also what the old dedup produced) — that is correct and expected; it
is a guard against the *new* code regressing the bound, and Step 6 proves it can fail.

- [ ] **Step 3: Make the production change**

In `apps/agent_worker/src/agent_worker/observer.py`, replace lines 556–565 — the whole skip branch:

```python
    async def _record_locked(self, answer: ExtractedAnswer, evidence_seq: int | None) -> None:
        if self._on_file.get(answer.field_path) == answer.value:
            # Unchanged — skip the write and the emit either way, so a rep merely confirming
            # a prefilled value still leaves no ai_call row (the INTAKE row stays current).
            # But the controller must still learn it: `gating_seed` drops ask-role prefills
            # from its baseline, so this is the only place left that can tell it the call
            # itself stated the value — otherwise the field is owed for the rest of the call.
            if self._recorded.get(answer.field_path) != answer.value:
                self._push_recorded(answer.field_path, answer.value)
            return
        ts = self._now_ms()
```

with:

```python
    async def _record_locked(self, answer: ExtractedAnswer, evidence_seq: int | None) -> None:
        # Dedup against what THIS CALL wrote, never against `_on_file`: that map is seeded from
        # `plan.prefilled`, which carries intake AND prior-call values, so keying on it discarded
        # the rep confirming one and left the earlier call owning the row. A repeat within this
        # call is still a no-op, which keeps the bound at one row per distinct value per call.
        if self._recorded.get(answer.field_path) == answer.value:
            return
        ts = self._now_ms()
```

Change nothing else. `_on_file[answer.field_path] = answer.value` at line 587 and
`_push_recorded(...)` at line 588 both stay exactly as they are.

- [ ] **Step 4: Run the three tests again**

```bash
uv run pytest apps/agent_worker/tests/unit/test_observer.py -k "repeating_a_prior_calls_value or confirming_a_confirm_role_prefill or repeated_prefill_still_writes" -v
```

Expected: all three PASS.

- [ ] **Step 5: Invert the test that asserted the defect**

`test_confirming_an_ask_role_prefill_still_reaches_the_controller` (line 242) now fails, because it
asserts `run_state.records == []`. Its *controller* claim is still correct and must survive.
Replace the whole test with:

```python
    @pytest.mark.asyncio
    async def test_confirming_an_ask_role_prefill_reaches_the_controller_and_the_row(self) -> None:
        # sections.a.x is `ask`-role (see `_field`), which `gating_seed` drops from the
        # controller's baseline — so the controller learning it is a real claim, not incidental.
        # It now learns it via the write path: `_push_recorded` runs after every successful
        # write, which is why deleting the old skip branch cost the controller nothing.
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Family", 90)])
        manager, run_state, bus, controller = _manager(
            _plan(prefilled={"sections.a.x": "Family"}), extractor
        )
        await _feed(manager, _rep("It's family coverage."))
        assert controller.answers["sections.a.x"] == "Family"
        # The confirmation is now provenance: an ai_call row under THIS call supersedes the
        # prefill, which is what lets an authoritative call verify a value it merely repeated.
        assert run_state.records == [(ROOM, "sections.a.x", "Family", 0)]
        assert len(bus.events) == 1
```

- [ ] **Step 6: Run the whole observer suite, then mutation-prove all four assertions**

```bash
uv run pytest apps/agent_worker/tests/unit/test_observer.py
```

Expected: all pass.

Now prove each new test can fail. **Mutation A** — restore the old dedup key in `_record_locked`:

```python
        if self._on_file.get(answer.field_path) == answer.value:
            return
```

Re-run the suite. Expected: `test_repeating_a_prior_calls_value_is_recorded_under_this_call`,
`test_confirming_a_confirm_role_prefill_is_recorded` and
`test_confirming_an_ask_role_prefill_reaches_the_controller_and_the_row` all FAIL. Restore.

**Mutation B** — delete the dedup guard entirely (first two lines of the body):

```python
    async def _record_locked(self, answer: ExtractedAnswer, evidence_seq: int | None) -> None:
        ts = self._now_ms()
```

Re-run. Expected: `test_a_repeated_prefill_still_writes_only_one_row_per_call` FAILS with
`assert 3 == 1`, and `test_unchanged_value_is_recorded_once` FAILS too. Restore.

Record both mutations and their observed failures in the ledger.

- [ ] **Step 7: Typecheck and lint the two changed files**

```bash
uv run mypy --strict
uv run ruff check apps/agent_worker/src/agent_worker/observer.py apps/agent_worker/tests/unit/test_observer.py
uv run ruff format --check apps/agent_worker/src/agent_worker/observer.py apps/agent_worker/tests/unit/test_observer.py
```

Expected: `Success: no issues found in 397 source files`, `All checks passed!`, `2 files already formatted`.

- [ ] **Step 8: Commit**

```bash
git add apps/agent_worker/src/agent_worker/observer.py apps/agent_worker/tests/unit/test_observer.py
git commit -m "fix(observer): dedup against what this call wrote, not what is on file

_on_file is seeded from plan.prefilled, which current_values_by_path builds from
EVERY current answer regardless of source — so on a retry it carries prior-call
values and the unchanged-skip fired far outside its documented intake-only
intent. A rep repeating a value written by a prior non-authoritative call had
the confirmation discarded, leaving that call owning the row and the field
Unverified forever, so verified_pct could not converge and the retry loop ran to
max_retries.

Dedup now keys on _recorded ('what THIS CALL collected'), so a repeat within the
call is still a no-op and the row-volume bound is unchanged, while a value first
stated on THIS call is always written. The deleted skip branch's _push_recorded
call is not needed: the surviving call site runs after every successful write,
so the controller still learns every value.

Also fixes a second, unrecorded defect. policy_number is role=confirm,
required=True, and _satisfied refuses to count an intake value for a confirm
leaf (tests/unit/forms/test_retryable_fields.py::test_intake_does_not_satisfy_a_confirm_leaf).
The skip meant a rep confirming the read-back member ID wrote no row, so the
field whose entire purpose is payer confirmation could never be satisfied by a
successful confirmation — only by the rep contradicting it."
```

---

## Task 3: Prove the write reaches the DB past the second dedup layer

**Files:**
- Modify: `tests/unit/services/test_field_answers.py`

**Interfaces:**
- Consumes: nothing from Task 2 (this tests a different layer).
- Produces: a `call_id` parameter on the file's existing `_current` helper.

**Background the implementer needs:**
`record_answer` has its own no-op guard (`field_answers.py:111`):

```python
    if current is not None and current.source == source and current.call_id == call_id:
```

Both conjuncts must hold for the replay short-circuit. On a new call `current.call_id` is the
PRIOR call's id, so it falls through to demote-then-insert; for an intake row `current.source` is
`intake` against a `source` of `ai_call`, so it falls through too. Task 2's extra writes therefore
reach the DB. That is currently an inference from reading — these tests make it evidence.

The existing `_current` helper hard-codes `call_id=CALL`. It needs a parameter.

- [ ] **Step 1: Add a `call_id` parameter to the existing `_current` helper**

In `tests/unit/services/test_field_answers.py`, change `_current` (line 48) to:

```python
def _current(
    value: Any,
    *,
    source: str = AnswerSource.AI_CALL.value,
    evidence_seq: int | None = None,
    call_id: Any = CALL,
) -> FieldAnswer:
    return FieldAnswer(
        tenant_id=TENANT,
        form_id=FORM,
        call_id=call_id,
        field_path="sections.a.x",
        value={"value": value},
        source=source,
        evidence_seq=evidence_seq,
        is_current=True,
    )
```

The default keeps every existing caller behaving identically.

- [ ] **Step 2: Write the two failing tests**

Append to `tests/unit/services/test_field_answers.py`:

```python
@pytest.mark.asyncio
async def test_same_value_from_a_different_call_supersedes() -> None:
    """The Observer now records a value the rep repeats from a prior call. The replay guard
    must NOT swallow it: it requires source AND call_id to match, and a new call's id differs,
    so the row is rewritten under the call that actually heard it. Without this the Observer
    fix would be invisible at the DB — the whole point is moving row ownership."""
    other_call = uuid4()
    session = _FakeSession(current=_current("Individual", call_id=other_call))
    assert await _record(session, "Individual") is True
    assert session.events == ["flush", "add"]  # demoted, flushed, then inserted
    assert session.current.is_current is False
    assert session.added[0].call_id == CALL
    assert session.added[0].source == AnswerSource.AI_CALL.value


@pytest.mark.asyncio
async def test_same_value_over_an_intake_row_supersedes() -> None:
    """The first-call case: intake typed the value, the rep confirmed it unchanged. The guard
    requires a matching source, and intake != ai_call, so the confirmation is written and the
    intake row is demoted. `baseline_value` filters on source and NOT on is_current, so the
    dispute baseline still resolves the demoted row (see test_review.py)."""
    session = _FakeSession(current=_current("Alpha", source=AnswerSource.INTAKE.value))
    assert await _record(session, "Alpha") is True
    assert session.events == ["flush", "add"]
    assert session.current.is_current is False
    assert session.added[0].source == AnswerSource.AI_CALL.value
```

`uuid4` is already imported in this file.

- [ ] **Step 3: Run them**

```bash
uv run pytest tests/unit/services/test_field_answers.py -v
```

Expected: **PASS** — this layer already behaves correctly; the tests convert an inference into
evidence. If either FAILS, stop: the spec's §3.4 claim is wrong and Task 2's fix does not reach the
DB. Report it rather than adjusting the test.

- [ ] **Step 4: Mutation-prove both**

In `packages/vera_core/src/vera_core/services/field_answers.py:111`, widen the guard to ignore
`call_id` and `source`:

```python
    if current is not None:
```

Re-run Step 3.
Expected: **both new tests FAIL** with `assert False is True` (the guard now returns `False` — a
no-op — because the stored value is identical). Several existing tests in the file will also fail;
that is fine and expected.

Restore line 111 exactly. Re-run: all pass.

Record the mutation and the observed failure in the ledger.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/services/test_field_answers.py
git commit -m "test(field-answers): pin that a repeated value from a new call supersedes

record_answer's replay guard requires source AND call_id to match. The Observer
fix relies on that falling through for a new call and for an intake row, which
was an inference from reading; these two tests make it evidence, so the fix
cannot be silently neutered one layer below where it was made.

Mutation-proved: widening the guard to `if current is not None` turns both into
no-ops and fails them."
```

---

## Task 4: Pin the dispute consequences

**Files:**
- Modify: `tests/unit/forms/test_review.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. Test-only.

**Background the implementer needs:**
Task 2 causes an `ai_call` row to supersede an intake row that holds the *same* value. Two things
must hold, and the second is the risky one.

`dispute_view` returns `None` when `normalize_value(current) == normalize_value(baseline)`
(`review.py:105`), and `normalize_value` strips ASCII whitespace and lowercases (`review.py:73-82`).
`baseline_value` filters on `source` and deliberately NOT on `is_current` (`field_answers.py:61-64`),
so the demoted intake row still resolves as the baseline. Equal values → no dispute.

But `canonical_answer` folds strictly MORE than `normalize_value` does: it also snaps onto authored
literals and parses currency (`answers.py:58-79`). Both writers canonicalize today
(`api/v1/patient_forms.py:262`, `observer.py:141`), so new data is safe. For a row written **before**
canonicalization existed, the fix writes the canonical spelling against a non-canonical baseline,
and a money-format or punctuation difference survives `normalize_value`. A spurious dispute blocks
form completion, so this case must be chosen deliberately, not discovered.

- [ ] **Step 1: Write the two tests**

Append to `class TestDisputeView` in `tests/unit/forms/test_review.py`:

```python
    def test_an_ai_call_row_repeating_the_intake_value_is_not_disputed(self) -> None:
        """The Observer now writes a row when the rep confirms a prefilled value unchanged.
        That must not manufacture a dispute: `baseline_value` filters on source and NOT on
        is_current, so the demoted intake row is still the baseline, and equal values compare
        equal. A spurious dispute would block form completion."""
        assert (
            dispute_view(
                source="ai_call",
                value={"value": "Alpha"},
                confidence=90,
                baseline_value={"value": "Alpha"},
            )
            is None
        )

    def test_a_pre_canonicalization_baseline_can_still_dispute_on_money_format(self) -> None:
        """Known, accepted edge. `canonical_answer` folds currency ($0.00 -> $0) but
        `normalize_value` only strips and lowercases, so a baseline stored BEFORE the writers
        canonicalized can diverge from the canonical value the Observer now writes. Both
        writers canonicalize today (patient_forms.py:262, observer.py:141), so this reaches
        legacy rows only. Pinned so the behaviour is chosen rather than discovered in prod."""
        assert dispute_view(
            source="ai_call",
            value={"value": "$0"},
            confidence=90,
            baseline_value={"value": "$0.00"},
        ) == {
            "previous_value": "$0.00",
            "current_value": "$0",
            "confidence": 90,
            "reasoning": None,
        }
```

- [ ] **Step 2: Run them**

```bash
uv run pytest tests/unit/forms/test_review.py::TestDisputeView -v
```

Expected: both PASS.

- [ ] **Step 3: Mutation-prove both**

In `packages/vera_core/src/vera_core/forms/review.py:105`, drop the normalization:

```python
    if unwrap_value(value) == unwrap_value(baseline_value):
```

Re-run Step 2. Expected: the *existing* `test_matching_baseline_is_none` sibling cases that rely on
case/whitespace folding FAIL. If the first new test still passes here (its values are
byte-identical), apply this second mutation instead — invert the early return at `review.py:105`:

```python
    if normalize_value(unwrap_value(value)) != normalize_value(unwrap_value(baseline_value)):
        return None
```

Expected: `test_an_ai_call_row_repeating_the_intake_value_is_not_disputed` FAILS (a dispute payload
is returned where `None` was asserted) **and**
`test_a_pre_canonicalization_baseline_can_still_dispute_on_money_format` FAILS (`None` returned
where a payload was asserted). One mutation, both tests red — that is the proof.

Restore `review.py:105`. Re-run: pass.

Record which mutation was used and the observed failures in the ledger.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/forms/test_review.py
git commit -m "test(review): pin dispute behaviour for a repeated-value ai_call row

The Observer fix makes an ai_call row supersede an intake row holding the same
value. Two consequences now have tests: equal values still produce no dispute
(baseline_value ignores is_current, so the demoted intake row remains the
baseline), and the known legacy edge where a pre-canonicalization baseline can
diverge on currency format is asserted explicitly rather than left to be found
in production, since a spurious dispute blocks form completion.

Mutation-proved: inverting the early return at review.py:105 fails both."
```

---

## Task 5: Pin the accepted regression — an unjudged ai_call row is not satisfied

**Files:**
- Modify: `tests/unit/forms/test_retryable_fields.py:71-76`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. Test-only.

**Background the implementer needs:**
`is_field_satisfied` is asymmetric (`review.py:263-266`): `intake`/`human` is unconditionally
`True`, `ai_call` requires `ai_supported` **and** `ai_confidence >= floor`. Task 2 moves many more
fields from `intake` to `ai_call`, so they become judge-conditional. `load_field_status` maps an
answer with no evaluation to `ai_supported=None`, and its own comment says that "already fails the
gate" (`field_status.py:66-69`).

On the normal path this is invisible — `evaluate_call` is "extract, persist, judge, and update
status" in one transaction (`post_call_eval.py:148`), so the judge has written `ai_supported` before
`unsatisfied` is computed. The exposure is the fallback path (`resolve_ai_processing`, reached when
`post_call_eval_ready` is false), where no judge runs at all.

The existing `test_is_field_satisfied_rules` covers `human`, `ai(90)`, `ai(60)`, `ai(90, sup=False)`
and `None` — but **not** `ai_supported=None`. Add that one case rather than a new test.

- [ ] **Step 1: Add the assertion**

In `tests/unit/forms/test_retryable_fields.py`, extend `test_is_field_satisfied_rules` (line 71):

```python
def test_is_field_satisfied_rules() -> None:
    assert is_field_satisfied(_human(), floor=FLOOR) is True  # trusted
    assert is_field_satisfied(_ai(90), floor=FLOOR) is True  # ai supported, >=70
    assert is_field_satisfied(_ai(60), floor=FLOOR) is False  # ai <70
    assert is_field_satisfied(_ai(90, sup=False), floor=FLOOR) is False  # unsupported
    assert is_field_satisfied(None, floor=FLOOR) is False  # unfilled (no status)
    # Unjudged: load_field_status yields ai_supported=None for an answer with no evaluation.
    # This is the accepted cost of the Observer recording confirmations — an intake value the
    # rep confirms becomes ai_call and so judge-conditional. Invisible on the normal path
    # (evaluate_call judges before computing `unsatisfied`, one transaction), but PERMANENT on
    # the fallback path, where no judge ever runs.
    assert is_field_satisfied(FieldStatus("ai_call", None, 95), floor=FLOOR) is False
```

`FieldStatus` is already imported in this file.

- [ ] **Step 2: Run it**

```bash
uv run pytest tests/unit/forms/test_retryable_fields.py::test_is_field_satisfied_rules -v
```

Expected: PASS.

- [ ] **Step 3: Mutation-prove the new assertion specifically**

In `packages/vera_core/src/vera_core/forms/review.py:266`, treat an unjudged answer as supported:

```python
        return (status.ai_supported is not False) and (status.ai_confidence or 0) >= floor
```

Re-run Step 2.
Expected: **FAIL** on the new line — `assert True is False`. The other five assertions still pass,
which is what shows the new line is carrying its own weight rather than riding on a sibling.

Restore `review.py:266`. Re-run: PASS.

Record the mutation and the observed failure in the ledger.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/forms/test_retryable_fields.py
git commit -m "test(review): pin that an unjudged ai_call answer is not satisfied

is_field_satisfied trusts intake unconditionally but gates ai_call on the judge,
so the Observer fix moves confirmed prefills from trusted to judge-conditional.
Harmless on the normal path (evaluate_call judges before computing unsatisfied,
same transaction) and permanent on the fallback path, where no judge runs. The
existing rules test covered every case except ai_supported=None; it does now, so
the accepted cost is recorded behaviour instead of a production discovery.

Mutation-proved: relaxing review.py:266 to `is not False` fails the new
assertion alone."
```

---

## Task 6: Full gates, simplify pass, and ledger

**Files:**
- Modify: whatever `/simplify` touches (re-verify after).
- Append: `.superpowers/sdd/2026-08-25-retry-decision-backend/progress.md` (git-ignored — the append will not appear in `git status`; do not try to commit it).

- [ ] **Step 1: Run the `/simplify` skill on the change**

Per the repo rule, run `/simplify` over the diff (`git diff 11213bec..HEAD`). Quality only — reuse,
simplification, altitude — never behaviour change.

- [ ] **Step 2: Run the full gate verbatim**

```bash
just check
```

Expected: **2708 passed + the net-new tests, 0 failed, 0 errors.** Net-new count: Task 2 adds 3,
Task 3 adds 2, Task 4 adds 2 → **2715 passed**, with Tasks 1 and 5 modifying existing tests rather
than adding any. If the arithmetic does not land exactly, work out why before proceeding — an
unexpected count means a test was silently dropped or duplicated.

If anything is red: the branch has **no** residue allowance any more (the post-merge baseline was
0 failed / 0 errors). Do not wave a failure through as pre-existing. If you suspect test-DB residue
in `vera_retry_call_fix_test`, the clear is `TRUNCATE patient_form CASCADE; TRUNCATE auth_audit_log;
TRUNCATE app_user CASCADE;` — note it also drops a `form_schema`, so re-seed afterwards.

- [ ] **Step 3: Confirm the frontend is untouched**

```bash
git diff --name-only 11213bec..HEAD -- ../vera-frontend
```

Expected: **empty**. This change is backend-only; realtime pill clearing is deferred (spec §4.1).

- [ ] **Step 4: Write the mutation evidence into the ledger**

Append a section to `.superpowers/sdd/2026-08-25-retry-decision-backend/progress.md` recording, for
each of the six mutations run in Tasks 1–5: the exact mutation, the test(s) that went red, and the
observed failure message. Git history holds the code; only the ledger holds this.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore(observer): simplify pass and full-gate verification

just check: <paste the verbatim summary line>
mypy --strict: <paste>
Frontend untouched — realtime pill clearing is deferred (spec 4.1)."
```

---

## Task 7: The live gate — REQUIRED, and a human must run it

**`just check` is not sufficient.** This changes voice-path behaviour: the assertions above are on
fakes, and the defect was found in production, not in a test. Do not report the work complete until
this passes.

- [ ] **Step 1: Bring up the stack with browser-callee transport**

```bash
just up
```

Then, with `VERA_BROWSER_CALLEE_TRANSPORT=true` exported for both:

```bash
just api
just worker
```

Frontend: `VITE_BROWSER_CALLEE_TRANSPORT=true`.

Constraints that bite: a **~60s join window** per call, and **one tab per call**.

- [ ] **Step 2: Build the scenario — a form with a prior NON-authoritative call**

The scenario is precisely the one in the brief's evidence: call 1 collects answers but captures
**no** call reference number, so it is non-authoritative; call 2 then runs FULL from the top
(`has_call_reference` is false, so the plan is not narrowed) and the rep **repeats** call 1's
values.

If you use `just seed-retry-form`: it **rewrites tenant config** and has silently reset a tuned
`retry_fill_threshold` mid-test before. **Re-apply tenant settings AFTER seeding**, and re-check
them immediately before the call.

- [ ] **Step 3: Take call 1 — answer several fields, and do NOT give a reference number**

Join as the payer rep. Answer a handful of ordinary `ask` fields. When asked for a call reference
number, decline or end the call before it is captured. Confirm afterwards that
`field_answer` has **no** row for the schema's `rep_call_reference_number_field` with a `call_id`.

- [ ] **Step 4: Take call 2 — repeat the same values verbatim, and DO give a reference number**

Join again. When asked, give **exactly the same answers as call 1**. Capture the call reference
number this time.

- [ ] **Step 5: Verify — this is the gate**

In the DB, for each field the rep repeated:

- a `field_answer` row exists whose `call_id` is **call 2**, and it is `is_current = true`
  (before the fix, call 1 would still own the current row);
- the form detail shows the field **without** the Unverified pill;
- `patient_form.verified_pct` rose.

Cross-check in Langfuse that call 2 emitted a `vera.observer.answer_recorded` span for each repeated
path — the brief proved the defect by those spans being ABSENT, so their presence is the direct
inverse. `observations.input`/`output` are NULL in ClickHouse (payloads live in MinIO), so use span
**presence**, not content:

```bash
docker exec vera-backend-langfuse-clickhouse-1 clickhouse-client
```

- [ ] **Step 6: Secondary observation, same call, no extra setup**

Confirm `policy_number` got a row from call 2 when the rep **confirmed** the read-back member ID
rather than contradicting it (spec §1.1). If the bot never read it back, note that and move on —
this is an observation, not a blocker.

- [ ] **Step 7: Record the result in the ledger**

Append: call ids, form id, the Langfuse trace id, the per-field before/after row ownership, and the
`verified_pct` movement. Note explicitly that the Unverified pill did **not** clear *during* the
call — that is expected and deferred (spec §4.1), not a failure of this fix.

---

## Self-Review

**Spec coverage.** §2.1 → Task 2. §2.3/§2.4 → Task 2 (unchanged lines called out explicitly).
§3.2 row-volume bound → Task 2 test 3 + Mutation B. §3.3 disputes, both cases → Task 4. §3.4 DB
reach → Task 3. §3.5 accepted regression → Task 5. §5.1 all three existing tests → Task 1
(re-point) and Task 2 (invert; `test_unchanged_value_is_recorded_once` verified untouched via
Mutation B). §5.2 items 1, 2a, 3, 4, 5, 6, 7 → Tasks 2–5. §5.3 gates → Task 6. Live gate → Task 7.
§7 follow-ups → carried in the spec, nothing to implement.

**One spec item deliberately not implemented:** §5.2 item 2(b), the review-level half of the
confirm-role cure, is **already covered** by `test_intake_does_not_satisfy_a_confirm_leaf` and
`test_a_call_satisfies_a_confirm_leaf` (`tests/unit/forms/test_retryable_fields.py:175,184`). Adding
it would duplicate. Task 2's test cites them instead, so the chain is legible end to end.

**Placeholder scan:** none. Every code step carries the literal code; every run step carries the
exact command and the expected output.

**Type consistency:** `_current(..., call_id=...)` added in Task 3 Step 1 is used in Task 3 Step 2.
`_field(path, role="confirm")` in Task 2 matches `_field`'s `**overrides` signature
(`test_observer.py:31`). `FieldStatus("ai_call", None, 95)` in Task 5 matches the positional order
`(source, ai_supported, ai_confidence, call_id=None)` (`review.py:251-254`). `FlowRule`,
`Comparison`, `Terminate` used in Task 1 are all already imported in `test_observer.py:12,25`.
