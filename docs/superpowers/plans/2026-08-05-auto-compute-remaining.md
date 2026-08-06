# Auto-Computed Remaining for Money Triplets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Total and Met are known for a money triplet, the agent worker computes and records `Remaining = Total − Met` as a normal answer, so it fills the live form in real time and the gap pass stops chasing a number we already have.

**Architecture:** A pure `derive_remaining` helper joins the existing `vera_core.forms.consistency` module; the Observer's locked record path gains a derivation step keyed on the CallPlan's `NumericConsistency` rules (the 4 missing rules get authored, so all 5 triplets are declared). The derived answer is recorded through the same routine as extracted answers, riding the existing persistence/SSE/validation pipeline unchanged. Fill-gaps-only: a per-run map of last-derived values ensures rep-stated or prefilled Remainings are never overwritten. Spec: `docs/superpowers/specs/2026-08-05-auto-compute-remaining-design.md`.

**Tech Stack:** Python 3.12 / pydantic v2 / pytest-asyncio (backend, uv workspace `vera-backend/`). No frontend or DB code changes (the compiled schema artifact regenerates).

## Global Constraints

- Work on branch `feat/auto-compute-remaining` (already created, stacked on `fix/lifetime-max-consistency`; the spec is committed on it).
- Backend gate: `just check` from `vera-backend/`, verbatim — never a subset. Frontend gate (from `vera-frontend/`): `npx tsc -b` + `npx eslint .` + `npm test` + `npm run build` — all four (the artifact its tests import changes in Task 3).
- **Never hand-edit `vera-backend/data/form_schemas/*.json`** — change the catalog, then `just compile-schemas`.
- Derived value format exactly `f"${total - met:,.2f}"` (e.g. `$20,000.00`); derive only when both inputs parse via the existing `parse_currency` and `0 ≤ met ≤ total`.
- Fill-gaps-only: write Remaining only when it is blank or equals the value this run last derived for that path; a different existing value permanently stops derivation for that path.
- Derived answers inherit the triggering answer's `confidence` and `evidence_seq`, and are recorded through the SAME locked routine as extracted answers (idempotent early-return on unchanged values).
- mypy strict; test functions `-> None`; comments only for non-obvious constraints, one line; docstrings one sentence.
- Git commits end with the two trailer lines used on this branch (`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` and the `Claude-Session:` URL). Pushes use the explicit refspec `git push origin HEAD:refs/heads/feat/auto-compute-remaining`.

---

### Task 1: `derive_remaining` helper (`vera_core.forms.consistency`)

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/consistency.py` (append after `check_triplet`)
- Test: `vera-backend/tests/unit/forms/test_consistency.py` (append)

**Interfaces:**
- Consumes: existing `parse_currency(value: str) -> float | None` in the same module.
- Produces: `derive_remaining(total_raw: str, met_raw: str) -> str | None` — formatted remaining, or `None` when either input doesn't parse or `met` is out of bounds.

- [ ] **Step 1: Write the failing tests**

Append to `vera-backend/tests/unit/forms/test_consistency.py` (extend the existing import line to include `derive_remaining`):

```python
class TestDeriveRemaining:
    def test_computes_and_formats_total_minus_met(self) -> None:
        assert derive_remaining("$25,000", "$5,000") == "$20,000.00"
        assert derive_remaining("100", "49.50") == "$50.50"

    def test_met_equal_to_total_gives_zero(self) -> None:
        assert derive_remaining("$500", "$500") == "$0.00"

    def test_zero_total_and_zero_met(self) -> None:
        assert derive_remaining("$0", "$0") == "$0.00"

    def test_met_exceeding_total_is_not_derived(self) -> None:
        assert derive_remaining("$100", "$300") is None

    def test_negative_met_is_not_derived(self) -> None:
        assert derive_remaining("$100", "-50") is None

    def test_unparseable_inputs_are_not_derived(self) -> None:
        assert derive_remaining("No Limit", "$300") is None
        assert derive_remaining("$100", "") is None
        assert derive_remaining("", "$50") is None
        assert derive_remaining("Unlimited", "Met") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from `vera-backend/`): `uv run pytest tests/unit/forms/test_consistency.py -k DeriveRemaining -v`
Expected: FAIL — `ImportError: cannot import name 'derive_remaining'`

- [ ] **Step 3: Write the implementation**

Append to `vera-backend/packages/vera_core/src/vera_core/forms/consistency.py`:

```python
def derive_remaining(total_raw: str, met_raw: str) -> str | None:
    """Remaining = total − met as a "$1,234.56" string; None unless 0 ≤ met ≤ total."""
    total = parse_currency(total_raw)
    met = parse_currency(met_raw)
    if total is None or met is None or met < 0 or met > total:
        return None
    return f"${total - met:,.2f}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/forms/test_consistency.py -v`
Expected: all PASS (new and pre-existing).

- [ ] **Step 5: Lint, format, commit**

Run: `uv run ruff check packages/vera_core/src/vera_core/forms/consistency.py tests/unit/forms/test_consistency.py && uv run ruff format packages/vera_core/src/vera_core/forms/consistency.py tests/unit/forms/test_consistency.py`

```bash
git add packages/vera_core/src/vera_core/forms/consistency.py tests/unit/forms/test_consistency.py
git commit -m "feat(forms): derive_remaining money-triplet helper"
```

---

### Task 2: Observer derivation step

**Files:**
- Modify: `vera-backend/apps/agent_worker/src/agent_worker/observer.py` (`ObserverManager.__init__` ends at :333 with `self._answers = dict(plan.prefilled)`; `_record_locked` is :433-488)
- Test: `vera-backend/apps/agent_worker/tests/unit/test_observer.py` (harness: `_plan` at :34, `_manager` at :136, `_feed`/`_settle` at :156-163; `FakeExtractor.answers` is a mutable list re-read per pass)

**Interfaces:**
- Consumes: `derive_remaining` (Task 1) and existing `triplet_paths` from `vera_core.forms.consistency`; `plan.numeric_consistencies` (parent branch); the existing `ExtractedAnswer` dataclass.
- Produces: after any recorded answer whose path is a declared triplet's `total` or `met_amount`, a blank (or previously-derived) `remaining` is recorded via `_record_locked` with the triggering answer's confidence/evidence_seq. Recursion depth is bounded at 1: the derived answer's own path is never a `total`/`met_amount` input.

- [ ] **Step 1: Write the failing tests**

In `test_observer.py`: extend the dsl import (line 23) to `from vera_core.forms.dsl import Comparison, FlowRule, NumericConsistency`, extend the directives import (line 11) to `from agent_worker.directives import ReAsk, Terminate`, then extend `_plan` (line 34) — add currency triplet fields to task `t1` and two new keyword params:

```python
def _plan(
    *,
    flow_rules: list[FlowRule] | None = None,
    numeric_consistencies: list[NumericConsistency] | None = None,
    prefilled: dict[str, Any] | None = None,
) -> CallPlan:
    return CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[
            PlanTask(
                task_key="t1",
                title="T1",
                prompt=".",
                fields=[
                    _field("sections.a.x"),
                    _field("sections.a.total"),
                    _field("sections.a.met_amount"),
                    _field("sections.a.remaining"),
                ],
            ),
            PlanTask(task_key="t2", title="T2", prompt=".", fields=[_field("sections.b.y")]),
        ],
        flow_rules=flow_rules or [],
        numeric_consistencies=numeric_consistencies or [],
        prefilled=prefilled or {},
    )
```

Append the test class:

```python
TRIPLET_RULE = NumericConsistency(rule_key="a_triplet", triplet="sections.a")
TOTAL, MET, REMAINING = "sections.a.total", "sections.a.met_amount", "sections.a.remaining"


class TestDerivedRemaining:
    @pytest.mark.asyncio
    async def test_derives_remaining_when_total_and_met_are_recorded(self) -> None:
        extractor = FakeExtractor(
            [ExtractedAnswer(TOTAL, "$25,000", 90), ExtractedAnswer(MET, "$5,000", 80)]
        )
        manager, run_state, bus, controller = _manager(
            _plan(numeric_consistencies=[TRIPLET_RULE]), extractor
        )
        await _feed(manager, _rep("Total is 25k, met 5k."))
        recorded = {path: value for _, path, value, _ in run_state.records}
        assert recorded[REMAINING] == "$20,000.00"
        assert controller.answers[REMAINING] == "$20,000.00"
        derived_events = [e for e in bus.events if e.field_path == REMAINING]
        assert len(derived_events) == 1
        assert derived_events[0].confidence == 80  # inherited from the triggering answer
        assert controller.applied == []  # a derived value never fires the triplet's ReAsk

    @pytest.mark.asyncio
    async def test_rep_stated_remaining_wins_and_stops_derivation(self) -> None:
        extractor = FakeExtractor(
            [
                ExtractedAnswer(TOTAL, "$25,000", 90),
                ExtractedAnswer(MET, "$5,000", 90),
                ExtractedAnswer(REMAINING, "$20,000", 90),
            ]
        )
        manager, run_state, _, controller = _manager(
            _plan(numeric_consistencies=[TRIPLET_RULE]), extractor
        )
        await _feed(manager, _rep("All three amounts."))
        assert controller.answers[REMAINING] == "$20,000"  # spoken value is current
        # A later Met correction must NOT recompute over the rep-stated value.
        extractor.answers = [ExtractedAnswer(MET, "$6,000", 90)]
        await _feed(manager, _rep("Correction: met is 6k.", ts=2))
        assert controller.answers[REMAINING] == "$20,000"

    @pytest.mark.asyncio
    async def test_recomputes_when_inputs_change_and_remaining_is_still_derived(self) -> None:
        extractor = FakeExtractor(
            [ExtractedAnswer(TOTAL, "$25,000", 90), ExtractedAnswer(MET, "$5,000", 90)]
        )
        manager, _, _, controller = _manager(
            _plan(numeric_consistencies=[TRIPLET_RULE]), extractor
        )
        await _feed(manager, _rep("Total 25k, met 5k."))
        assert controller.answers[REMAINING] == "$20,000.00"
        extractor.answers = [ExtractedAnswer(MET, "$6,000", 90)]
        await _feed(manager, _rep("Correction: met is 6k.", ts=2))
        assert controller.answers[REMAINING] == "$19,000.00"

    @pytest.mark.asyncio
    async def test_prefilled_remaining_blocks_derivation(self) -> None:
        extractor = FakeExtractor(
            [ExtractedAnswer(TOTAL, "$25,000", 90), ExtractedAnswer(MET, "$5,000", 90)]
        )
        manager, run_state, _, controller = _manager(
            _plan(
                numeric_consistencies=[TRIPLET_RULE],
                prefilled={REMAINING: "$1,000"},
            ),
            extractor,
        )
        await _feed(manager, _rep("Total 25k, met 5k."))
        assert controller.answers[REMAINING] == "$1,000"
        assert not any(path == REMAINING for _, path, _, _ in run_state.records)

    @pytest.mark.asyncio
    async def test_impossible_inputs_derive_nothing_and_reask_fires(self) -> None:
        extractor = FakeExtractor(
            [ExtractedAnswer(TOTAL, "$100", 90), ExtractedAnswer(MET, "$300", 90)]
        )
        manager, run_state, _, controller = _manager(
            _plan(numeric_consistencies=[TRIPLET_RULE]), extractor
        )
        await _feed(manager, _rep("Total 100, met 300."))
        assert not any(path == REMAINING for _, path, _, _ in run_state.records)
        assert any(isinstance(d, ReAsk) for d in controller.applied)

    @pytest.mark.asyncio
    async def test_repeated_passes_record_the_derived_value_once(self) -> None:
        extractor = FakeExtractor(
            [ExtractedAnswer(TOTAL, "$25,000", 90), ExtractedAnswer(MET, "$5,000", 90)]
        )
        manager, run_state, _, _ = _manager(
            _plan(numeric_consistencies=[TRIPLET_RULE]), extractor
        )
        await _feed(manager, _rep("Total 25k, met 5k."))
        await _feed(manager, _rep("Same again.", ts=2))
        derived = [r for r in run_state.records if r[1] == REMAINING]
        assert len(derived) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest apps/agent_worker/tests/unit/test_observer.py -k Derived -v`
Expected: FAIL — `recorded[REMAINING]` raises `KeyError` / assertions on missing derivation (the `_plan` change itself must not break other observer tests — run the whole file once here too and confirm only the new class fails).

- [ ] **Step 3: Implement the derivation step**

In `observer.py`, extend the consistency import (add to the existing `vera_core.forms` imports):

```python
from vera_core.forms.consistency import derive_remaining, triplet_paths
```

In `ObserverManager.__init__`, directly after `self._answers: dict[str, Any] = dict(plan.prefilled)` (line 333):

```python
        self._triplets = [triplet_paths(rule.triplet) for rule in plan.numeric_consistencies]
        self._derived: dict[str, str] = {}
```

At the end of `_record_locked` (after the rule-engine span block, i.e. after the `apply_directive_now` line at :488), add the call and the new method:

```python
        await self._derive_remaining_locked(answer, evidence_seq)

    async def _derive_remaining_locked(
        self, trigger: ExtractedAnswer, evidence_seq: int | None
    ) -> None:
        """Fill a triplet's blank remaining with total − met (fill-gaps-only)."""
        for total_path, met_path, remaining_path in self._triplets:
            if trigger.field_path not in (total_path, met_path):
                continue
            current = self._answers.get(remaining_path)
            occupied = current is not None and str(current).strip() != ""
            if occupied and current != self._derived.get(remaining_path):
                continue  # a rep-stated or prefilled remaining wins — never overwrite it
            value = derive_remaining(
                str(self._answers.get(total_path) or ""),
                str(self._answers.get(met_path) or ""),
            )
            if value is None:
                continue
            self._derived[remaining_path] = value
            await self._record_locked(
                ExtractedAnswer(remaining_path, value, trigger.confidence), evidence_seq
            )
```

Note the recursion is bounded: the recursive `_record_locked` call's path is a `remaining`, which matches no rule's `total`/`met_amount`, so its own `_derive_remaining_locked` pass is a no-op. The unchanged-value early-return at the top of `_record_locked` makes repeated derivations idempotent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest apps/agent_worker/tests/unit/test_observer.py -v`
Expected: all PASS (new class and all pre-existing observer tests).

- [ ] **Step 5: Lint, format, mypy, commit**

Run: `uv run ruff check apps/agent_worker/src/agent_worker/observer.py apps/agent_worker/tests/unit/test_observer.py && uv run ruff format apps/agent_worker/src/agent_worker/observer.py apps/agent_worker/tests/unit/test_observer.py && uv run mypy`
Expected: clean (mypy zero errors).

```bash
git add apps/agent_worker/src/agent_worker/observer.py apps/agent_worker/tests/unit/test_observer.py
git commit -m "feat(agent): auto-derive money-triplet remaining in the observer"
```

---

### Task 3: Author the 4 missing catalog rules + recompile

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py` (`numeric_consistencies=[...]` block at :1364-1375)
- Regenerate: `vera-backend/data/form_schemas/ibv_form_standard_v2.json` (via `just compile-schemas` only)
- Test: `vera-backend/tests/unit/forms/test_call_plan.py` (`test_plan_carries_numeric_consistencies`)

**Interfaces:**
- Consumes: existing `NumericConsistency` model; the deductible/OOP triplet groups (`sections.deductibles.individual|family`, `sections.out_of_pocket.individual|family` — built by `money_triplet`, children `total`/`met_amount`/`remaining`, all currency).
- Produces: the shipped ibv schema declares 5 rules; rule_keys in artifact order: `lifetime_maximum_triplet_consistency`, `deductible_individual_triplet_consistency`, `deductible_family_triplet_consistency`, `oop_individual_triplet_consistency`, `oop_family_triplet_consistency`.

- [ ] **Step 1: Update the carry test to expect 5 rules (failing first)**

Replace the body of `test_plan_carries_numeric_consistencies` in `test_call_plan.py`:

```python
def test_plan_carries_numeric_consistencies() -> None:
    assert [(r.rule_key, r.triplet) for r in PLAN.numeric_consistencies] == [
        ("lifetime_maximum_triplet_consistency", "sections.lifetime_maximum"),
        ("deductible_individual_triplet_consistency", "sections.deductibles.individual"),
        ("deductible_family_triplet_consistency", "sections.deductibles.family"),
        ("oop_individual_triplet_consistency", "sections.out_of_pocket.individual"),
        ("oop_family_triplet_consistency", "sections.out_of_pocket.family"),
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/forms/test_call_plan.py::test_plan_carries_numeric_consistencies -v`
Expected: FAIL — the artifact carries only the lifetime-maximum rule.

- [ ] **Step 3: Author the 4 rules**

In `ibv_standard.py`, inside the existing `numeric_consistencies=[...]` list, after the lifetime-maximum rule's closing `),`:

```python
            NumericConsistency(
                rule_key="deductible_individual_triplet_consistency",
                triplet="sections.deductibles.individual",
                clarify=(
                    "Could you double-check the total individual deductible, how much "
                    "of it has been met, and how much remains?"
                ),
            ),
            NumericConsistency(
                rule_key="deductible_family_triplet_consistency",
                triplet="sections.deductibles.family",
                clarify=(
                    "Could you double-check the total family deductible, how much of "
                    "it has been met, and how much remains?"
                ),
            ),
            NumericConsistency(
                rule_key="oop_individual_triplet_consistency",
                triplet="sections.out_of_pocket.individual",
                clarify=(
                    "Could you double-check the total individual out-of-pocket "
                    "maximum, how much of it has been met, and how much remains?"
                ),
            ),
            NumericConsistency(
                rule_key="oop_family_triplet_consistency",
                triplet="sections.out_of_pocket.family",
                clarify=(
                    "Could you double-check the total family out-of-pocket maximum, "
                    "how much of it has been met, and how much remains?"
                ),
            ),
```

- [ ] **Step 4: Recompile and verify artifact scope**

Run: `just compile-schemas`, then `git diff --stat data/`
Expected: only `ibv_form_standard_v2.json` changed (the added rules); `disease_only_verification.json` untouched.

- [ ] **Step 5: Run the forms suite (freshness + round-trip + carry)**

Run: `uv run pytest tests/unit/forms/ -q && uv run ruff check packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py tests/unit/forms/test_call_plan.py && uv run ruff format packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py tests/unit/forms/test_call_plan.py`
Expected: all PASS, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py data/form_schemas/ibv_form_standard_v2.json tests/unit/forms/test_call_plan.py
git commit -m "feat(ibv): declare consistency rules for all five money triplets"
```

---

### Task 4: Full verification, runtime exercise, wrap-up

**Files:** none new — gates over the whole branch.

- [ ] **Step 1: Backend gate**

Run (from `vera-backend/`): `just check`
Expected: all green (ruff check, ruff format --check, mypy --strict, pytest). If the venv is stale, `uv sync --all-packages` first — a stale `livekit-protocol` previously caused false turn-commit failures.

- [ ] **Step 2: Frontend gate — all four**

Run (from `vera-frontend/`): `npx tsc -b && npx eslint . && npm test -- --run && npm run build`
Expected: all green. The frontend code is untouched, but its tests import the recompiled artifact (now 5 rules) — existing tests must not start flagging deductible/OOP values (they only set totals, never conflicting met/remaining, so no new errors are expected).

- [ ] **Step 3: Runtime exercise (test it out)**

From `vera-backend/`, run this script with `uv run python -` and confirm the printed behavior:

```python
import uuid
from pathlib import Path
from vera_core.forms.dsl import load_document
from vera_core.forms.call_plan import compile_call_plan
from vera_core.forms.consistency import derive_remaining

doc = load_document(Path("data/form_schemas/ibv_form_standard_v2.json").read_text())
plan = compile_call_plan(doc, None, schema_version_id=uuid.uuid4(), prompt_version_id=None)
assert len(plan.numeric_consistencies) == 5, plan.numeric_consistencies
print("rules:", [r.rule_key for r in plan.numeric_consistencies])
print("derive:", derive_remaining("$25,000", "$5,000"))  # expect $20,000.00
```

Then exercise the Observer path end-to-end with the real plan (total+met recorded → derived remaining lands in the snapshot); the Task 2 tests already cover this with fakes, so a scripted spot-check against the real plan is sufficient here.

- [ ] **Step 4: Simplify pass (mandatory per repo CLAUDE.md)**

Run the `/simplify` skill on the branch's changes. After it applies refinements, re-run Steps 1-2 in full.

- [ ] **Step 5: Final whole-branch review**

Dispatch the final code reviewer over `git merge-base fix/lifetime-max-consistency HEAD`..HEAD (this branch's own commits only — the parent branch was already reviewed).

- [ ] **Step 6: Push and hand over the PR**

```bash
git push origin HEAD:refs/heads/feat/auto-compute-remaining
```

Open the PR from the URL Bitbucket prints. **Destination: `fix/lifetime-max-consistency`** (this branch stacks on it; retarget to `dev` after the parent PR merges) — or `dev` directly if the parent has already merged. Do NOT merge. PR body must note the manual pre-ship gates: an eval-harness scenario (rep gives Total and Met but never Remaining → form fills anyway; gap pass doesn't re-ask) and a live call.
