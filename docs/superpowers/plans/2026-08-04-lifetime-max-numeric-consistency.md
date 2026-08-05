# Lifetime Maximum Numeric-Consistency Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect logically impossible Lifetime Maximum values (Met > Total, Remaining > Total, Met + Remaining ≠ Total) during the live call, make the agent re-ask the representative, and flag unresolved inconsistencies in the review UI.

**Architecture:** One `NumericConsistency` rule declared in the form catalog flows into two consumers: (1) the agent worker's `RuleEngine`, which evaluates it after every recorded answer and fires the existing `ReAsk` directive with a dynamic reason quoting the rep's numbers; (2) the review UI's `validateAll`, which runs the same three checks and attaches errors to the triplet fields. Values are always stored (store-flag-reask; no write blocking). Spec: `docs/superpowers/specs/2026-08-04-lifetime-max-numeric-consistency-design.md`.

**Tech Stack:** Python 3.12 / pydantic v2 / pytest (backend, uv workspace `vera-backend/`), TypeScript / React / vitest (frontend `vera-frontend/`).

## Global Constraints

- Work on branch `fix/lifetime-max-consistency` (already created; spec is committed on it).
- Backend gate: `just check` (ruff check + ruff format --check + mypy --strict + pytest), run from `vera-backend/`, verbatim — never a subset.
- Frontend gate (from `vera-frontend/`): `npx tsc -b` + `npx eslint .` + `npm test` + `npm run build` — all four.
- **Never hand-edit `vera-backend/data/form_schemas/*.json`** — change the catalog, then `just compile-schemas`. The freshness test fails CI on drift.
- Comments only for non-obvious constraints, one line; docstrings one sentence (repo CLAUDE.md).
- mypy is strict: every test function is annotated `-> None`; use PEP 695 style, no `TypeVar`/`Generic`.
- Sum tolerance is ±$0.01 implemented float-noise-safe as `abs(round((met + remaining - total) * 100)) > 1` — **identical semantics in Python and TypeScript** (the two sides are a documented parity pair, like `conditions.py` ⇄ `conditions.ts`).
- Currency parsing: strip `$`, `,`, `%`, whitespace; empty/unparseable/non-finite → the value does not participate in any check. Each check runs only when all of its operands are numeric.
- Git commits end with the two trailer lines used on this branch (`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` and the `Claude-Session:` URL from the session).
- All git pushes use an explicit refspec (`git push origin HEAD:refs/heads/fix/lifetime-max-consistency`); never bare `git push` (it targets dev).

---

### Task 1: Consistency evaluation helper (`vera_core.forms.consistency`)

**Files:**
- Create: `vera-backend/packages/vera_core/src/vera_core/forms/consistency.py`
- Test: `vera-backend/tests/unit/forms/test_consistency.py`

**Interfaces:**
- Consumes: nothing (stdlib only — must NOT import `vera_core.forms.dsl`, because `dsl.py` will import `TRIPLET_KEYS` from here in Task 2).
- Produces:
  - `TRIPLET_KEYS: tuple[str, str, str] = ("total", "met_amount", "remaining")`
  - `parse_currency(value: str) -> float | None`
  - `triplet_paths(base: str) -> tuple[str, str, str]` — `(f"{base}.total", f"{base}.met_amount", f"{base}.remaining")`
  - `check_triplet(base: str, answers: Mapping[str, Any]) -> str | None` — dynamic human-readable reason on violation, `None` when consistent/insufficient data.

- [ ] **Step 1: Write the failing tests**

Create `vera-backend/tests/unit/forms/test_consistency.py`:

```python
"""Money-triplet consistency: currency parsing, partial data, violation reasons."""

from vera_core.forms.consistency import check_triplet, parse_currency, triplet_paths

BASE = "sections.lifetime_maximum"
TOTAL, MET, REMAINING = triplet_paths(BASE)


class TestParseCurrency:
    def test_parses_plain_and_formatted_amounts(self) -> None:
        assert parse_currency("300") == 300.0
        assert parse_currency("$1,500.50") == 1500.50
        assert parse_currency(" $25,000 ") == 25000.0

    def test_rejects_specials_prose_and_blanks(self) -> None:
        assert parse_currency("No Limit") is None
        assert parse_currency("Met") is None
        assert parse_currency("") is None
        assert parse_currency("call back later") is None

    def test_rejects_non_finite(self) -> None:
        assert parse_currency("inf") is None
        assert parse_currency("nan") is None


class TestCheckTriplet:
    def test_consistent_values_pass(self) -> None:
        answers = {TOTAL: "$25,000", MET: "$5,000", REMAINING: "$20,000"}
        assert check_triplet(BASE, answers) is None

    def test_met_exceeding_total_is_flagged_with_amounts(self) -> None:
        reason = check_triplet(BASE, {TOTAL: "$100", MET: "$300"})
        assert reason is not None
        assert "met amount ($300.00) exceeds the total ($100.00)" in reason

    def test_remaining_exceeding_total_is_flagged(self) -> None:
        reason = check_triplet(BASE, {TOTAL: "$100", REMAINING: "$300"})
        assert reason is not None
        assert "remaining amount ($300.00) exceeds the total ($100.00)" in reason

    def test_bug_report_example_flags_both_exceeds(self) -> None:
        reason = check_triplet(BASE, {TOTAL: "$100", MET: "$300", REMAINING: "$300"})
        assert reason is not None
        assert "met amount ($300.00) exceeds" in reason
        assert "remaining amount ($300.00) exceeds" in reason
        assert "does not match" not in reason  # exceed clauses suppress the sum clause

    def test_sum_mismatch_is_flagged_when_nothing_exceeds(self) -> None:
        reason = check_triplet(BASE, {TOTAL: "$25,000", MET: "$5,000", REMAINING: "$25,000"})
        assert reason is not None
        assert (
            "met amount ($5,000.00) plus the remaining amount ($25,000.00) "
            "does not match the total ($25,000.00)" in reason
        )

    def test_one_cent_rounding_is_tolerated(self) -> None:
        assert check_triplet(BASE, {TOTAL: "100", MET: "50", REMAINING: "50.01"}) is None
        assert check_triplet(BASE, {TOTAL: "100", MET: "50", REMAINING: "50.02"}) is not None

    def test_partial_data_never_fires(self) -> None:
        assert check_triplet(BASE, {}) is None
        assert check_triplet(BASE, {TOTAL: "$100"}) is None
        assert check_triplet(BASE, {MET: "$300", REMAINING: "$300"}) is None  # no total
        assert check_triplet(BASE, {TOTAL: "$100", MET: "$50"}) is None  # sum needs all 3

    def test_special_values_do_not_participate(self) -> None:
        assert check_triplet(BASE, {TOTAL: "No Limit", MET: "$300", REMAINING: "$300"}) is None
        assert check_triplet(BASE, {TOTAL: "$100", MET: "$50", REMAINING: "Met"}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from `vera-backend/`): `uv run pytest tests/unit/forms/test_consistency.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vera_core.forms.consistency'`

- [ ] **Step 3: Write the implementation**

Create `vera-backend/packages/vera_core/src/vera_core/forms/consistency.py`:

```python
"""Money-triplet numeric consistency: the pure checks behind NumericConsistency rules.

Backend half of a parity pair with the review UI's pass in
vera-frontend/src/lib/ibv/validation.ts — keep operand-skipping, tolerance and
message semantics in sync (same spirit as conditions.py ⇄ conditions.ts).
"""

import math
import re
from collections.abc import Mapping
from typing import Any

TRIPLET_KEYS: tuple[str, str, str] = ("total", "met_amount", "remaining")

_STRIP_RE = re.compile(r"[$,%\s]")


def parse_currency(value: str) -> float | None:
    """Parse a transcribed money string; None for specials, prose, or blanks."""
    cleaned = _STRIP_RE.sub("", value)
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def triplet_paths(base: str) -> tuple[str, str, str]:
    """The three leaf paths of the money triplet rooted at *base*."""
    return f"{base}.{TRIPLET_KEYS[0]}", f"{base}.{TRIPLET_KEYS[1]}", f"{base}.{TRIPLET_KEYS[2]}"


def _amount(answers: Mapping[str, Any], path: str) -> float | None:
    value = answers.get(path)
    return None if value is None else parse_currency(str(value))


def check_triplet(base: str, answers: Mapping[str, Any]) -> str | None:
    """Reason text when the triplet's recorded amounts are impossible, else None.

    Each check runs only when all of its operands parse as numbers; the sum check
    (±$0.01, compared in whole cents to dodge float noise) is skipped when an
    exceed check already fired — one clear clause beats a redundant second one.
    """
    total_path, met_path, remaining_path = triplet_paths(base)
    total = _amount(answers, total_path)
    met = _amount(answers, met_path)
    remaining = _amount(answers, remaining_path)

    clauses: list[str] = []
    if total is not None and met is not None and met > total:
        clauses.append(f"the met amount (${met:,.2f}) exceeds the total (${total:,.2f})")
    if total is not None and remaining is not None and remaining > total:
        clauses.append(
            f"the remaining amount (${remaining:,.2f}) exceeds the total (${total:,.2f})"
        )
    if (
        not clauses
        and total is not None
        and met is not None
        and remaining is not None
        and abs(round((met + remaining - total) * 100)) > 1
    ):
        clauses.append(
            f"the met amount (${met:,.2f}) plus the remaining amount (${remaining:,.2f}) "
            f"does not match the total (${total:,.2f})"
        )
    if not clauses:
        return None
    return "The recorded amounts are inconsistent: " + " and ".join(clauses) + "."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/forms/test_consistency.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/consistency.py tests/unit/forms/test_consistency.py
git commit -m "feat(forms): money-triplet numeric consistency checks"
```

---

### Task 2: DSL rule model + document validator

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py` (Contradiction is at :426-433; `FormSchemaDoc.contradictions` at :493; the validator's contradictions block ends at :807)
- Test: `vera-backend/tests/unit/forms/test_schema_dsl.py`

**Interfaces:**
- Consumes: `TRIPLET_KEYS` from `vera_core.forms.consistency` (Task 1).
- Produces: `NumericConsistency(_Model)` with fields `rule_key: str`, `triplet: str`, `clarify: str | None = None`; `FormSchemaDoc.numeric_consistencies: list[NumericConsistency] | None = None`. Validator rejects a rule whose `<triplet>.total|met_amount|remaining` is missing or not a `currency` leaf, and duplicate rule_keys (shared namespace with contradictions).

- [ ] **Step 1: Write the failing tests**

Add to `vera-backend/tests/unit/forms/test_schema_dsl.py` (uses the existing `minimal_doc` helper; keep the import of `ValidationError` that is already there):

```python
def triplet_doc(**overrides: Any) -> dict[str, Any]:
    """minimal_doc plus a currency triplet section and one NumericConsistency rule."""
    money = {"type": "currency", "title": "Money", "role": "ask", "prompt": {"ask": "How much?"}}
    doc = minimal_doc()
    doc["sections"]["ltm"] = {
        "title": "LTM",
        "fields": {
            "total": dict(money, title="Total"),
            "met_amount": dict(money, title="Met Amount"),
            "remaining": dict(money, title="Remaining"),
        },
    }
    doc["tasks"][0]["sections"].append("ltm")
    doc["numeric_consistencies"] = [{"rule_key": "ltm_consistency", "triplet": "sections.ltm"}]
    doc.update(overrides)
    return doc


class TestNumericConsistencyValidation:
    def test_valid_triplet_rule_is_accepted(self) -> None:
        doc = FormSchemaDoc.model_validate(triplet_doc())
        assert doc.numeric_consistencies is not None
        assert doc.numeric_consistencies[0].rule_key == "ltm_consistency"

    def test_missing_triplet_child_is_rejected(self) -> None:
        doc = triplet_doc()
        del doc["sections"]["ltm"]["fields"]["remaining"]
        with pytest.raises(ValidationError, match="sections.ltm.remaining.*not a leaf"):
            FormSchemaDoc.model_validate(doc)

    def test_non_currency_child_is_rejected(self) -> None:
        doc = triplet_doc()
        doc["sections"]["ltm"]["fields"]["met_amount"]["type"] = "text"
        with pytest.raises(ValidationError, match="sections.ltm.met_amount.*currency"):
            FormSchemaDoc.model_validate(doc)

    def test_duplicate_rule_key_is_rejected(self) -> None:
        doc = triplet_doc()
        doc["numeric_consistencies"].append(dict(doc["numeric_consistencies"][0]))
        with pytest.raises(ValidationError, match="duplicate.*ltm_consistency"):
            FormSchemaDoc.model_validate(doc)

    def test_round_trips_through_compile_and_load(self) -> None:
        doc = FormSchemaDoc.model_validate(triplet_doc())
        assert load_document(compile_document(doc)) == doc
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -k NumericConsistency -v`
Expected: FAIL — pydantic rejects the unknown `numeric_consistencies` key (`extra="forbid"`).

- [ ] **Step 3: Implement the model, field, and validator block**

In `dsl.py`, add the import (near the top, with the other stdlib/module imports):

```python
from vera_core.forms.consistency import TRIPLET_KEYS
```

Immediately after the `Contradiction` class (after line 433):

```python
class NumericConsistency(_Model):
    """Money-triplet consistency rule: met/remaining must not exceed total and
    met + remaining must equal total; a violation pushes back and re-clarifies."""

    rule_key: str
    triplet: str
    clarify: str | None = None
```

In `FormSchemaDoc`, directly after `contradictions: list[Contradiction] | None = None` (line 493):

```python
    numeric_consistencies: list[NumericConsistency] | None = None
```

In the document validator, directly after the contradictions block (after line 807, before `if errors:`) — note it reuses the `rule_keys` set the contradictions block builds, so the two rule kinds share one key namespace:

```python
        # numeric consistencies
        for rule in self.numeric_consistencies or []:
            rk = rule.rule_key
            check_key(f"numeric_consistency {rk}", rk)
            if rk in rule_keys:
                errors.append(f"duplicate rule_key {rk!r}")
            rule_keys.add(rk)
            for child in TRIPLET_KEYS:
                path = f"{rule.triplet}.{child}"
                if path not in leaves:
                    errors.append(f"numeric_consistency {rk}: {path!r} is not a leaf field")
                elif leaves[path].type != "currency":
                    errors.append(f"numeric_consistency {rk}: {path!r} must be a currency leaf")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -v`
Expected: all PASS, including the pre-existing freshness/round-trip tests (`exclude_none` compilation means artifacts without the new field are unchanged).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): NumericConsistency rule in the schema DSL"
```

---

### Task 3: Carry through CallPlan + evaluate in RuleEngine

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/call_plan.py` (dsl import block :40-51; `CallPlan.contradictions` at :108; `compile_call_plan`'s return at :171-187)
- Modify: `vera-backend/apps/agent_worker/src/agent_worker/rule_engine.py` (whole file is 62 lines)
- Test: `vera-backend/apps/agent_worker/tests/unit/test_rule_engine.py`

**Interfaces:**
- Consumes: `NumericConsistency` (Task 2), `check_triplet` / `triplet_paths` (Task 1), existing `ReAsk` directive.
- Produces: `CallPlan.numeric_consistencies: list[NumericConsistency]` (default `[]`); `RuleEngine.evaluate` returns `ReAsk(rule_key, reason=<dynamic>, clarify=rule.clarify)` on violation, evaluated after contradictions, with per-combination snapshot re-arm.

- [ ] **Step 1: Write the failing tests**

In `test_rule_engine.py`, extend the `_plan` helper and add tests. Replace the helper's signature/body:

```python
def _plan(
    *,
    flow_rules: list[FlowRule] | None = None,
    contradictions: list[Contradiction] | None = None,
    numeric_consistencies: list[NumericConsistency] | None = None,
) -> CallPlan:
    return CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[
            PlanTask(task_key="t1", title="T1", prompt="."),
            PlanTask(task_key="t2", title="T2", prompt="."),
        ],
        flow_rules=flow_rules or [],
        contradictions=contradictions or [],
        numeric_consistencies=numeric_consistencies or [],
    )
```

Update the imports line to include the new model:

```python
from vera_core.forms.dsl import Comparison, Contradiction, FlowRule, NumericConsistency
```

Append the tests:

```python
LTM = "sections.lifetime_maximum"
LTM_RULE = NumericConsistency(
    rule_key="ltm_consistency", triplet=LTM, clarify="Could you double-check those amounts?"
)


def test_numeric_consistency_fires_reask_with_dynamic_reason() -> None:
    engine = RuleEngine(_plan(numeric_consistencies=[LTM_RULE]))
    directive = engine.evaluate(
        {f"{LTM}.total": "$100", f"{LTM}.met_amount": "$300", f"{LTM}.remaining": "$300"}
    )
    assert isinstance(directive, ReAsk)
    assert directive.rule_key == "ltm_consistency"
    assert "$300.00" in directive.reason and "$100.00" in directive.reason
    assert directive.clarify == "Could you double-check those amounts?"


def test_numeric_consistency_silent_on_consistent_or_partial_values() -> None:
    engine = RuleEngine(_plan(numeric_consistencies=[LTM_RULE]))
    assert engine.evaluate({}) is None
    assert engine.evaluate({f"{LTM}.total": "$25,000"}) is None
    assert (
        engine.evaluate(
            {f"{LTM}.total": "$25,000", f"{LTM}.met_amount": "$5,000", f"{LTM}.remaining": "$20,000"}
        )
        is None
    )
    assert (
        engine.evaluate(
            {f"{LTM}.total": "No Limit", f"{LTM}.met_amount": "$300", f"{LTM}.remaining": "$300"}
        )
        is None
    )


def test_numeric_consistency_rearms_on_new_values_only() -> None:
    engine = RuleEngine(_plan(numeric_consistencies=[LTM_RULE]))
    bad = {f"{LTM}.total": "$100", f"{LTM}.met_amount": "$300", f"{LTM}.remaining": "$300"}
    assert engine.evaluate(bad) is not None
    # same impossible combination restated → do not badger the rep again
    assert engine.evaluate(bad) is None
    # a genuinely new impossible combination re-arms the push-back
    still_bad = dict(bad, **{f"{LTM}.met_amount": "$500"})
    assert engine.evaluate(still_bad) is not None


def test_contradiction_beats_numeric_consistency_in_the_same_pass() -> None:
    contradiction = Contradiction(
        rule_key="c",
        when=Comparison(field=f"{LTM}.total", op="eq", value="$100"),
        fields=[f"{LTM}.total"],
        reason="r",
    )
    engine = RuleEngine(
        _plan(contradictions=[contradiction], numeric_consistencies=[LTM_RULE])
    )
    directive = engine.evaluate(
        {f"{LTM}.total": "$100", f"{LTM}.met_amount": "$300", f"{LTM}.remaining": "$300"}
    )
    assert directive == ReAsk(rule_key="c", reason="r", clarify=None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest apps/agent_worker/tests/unit/test_rule_engine.py -v`
Expected: FAIL — `CallPlan` has no `numeric_consistencies` field / import error.

- [ ] **Step 3: Implement the carry and the evaluation loop**

`call_plan.py` — add `NumericConsistency` to the `vera_core.forms.dsl` import block (alphabetical, after `LeafType`); add to `CallPlan` after `contradictions` (line 108):

```python
    numeric_consistencies: list[NumericConsistency] = Field(default_factory=list)
```

and in `compile_call_plan`'s `CallPlan(...)` construction after `contradictions=...` (line 184):

```python
        numeric_consistencies=list(doc.numeric_consistencies or []),
```

`rule_engine.py` — add the import:

```python
from vera_core.forms.consistency import check_triplet, triplet_paths
```

In `__init__`, after `self._contradictions = plan.contradictions`:

```python
        self._numeric = plan.numeric_consistencies
```

and after `self._contradiction_snapshots ...`:

```python
        self._numeric_snapshots: dict[str, tuple[Any, ...]] = {}
```

In `evaluate`, replace the final `return None` with:

```python
        for rule in self._numeric:
            snapshot = tuple(answers.get(path) for path in triplet_paths(rule.triplet))
            if snapshot == self._numeric_snapshots.get(rule.rule_key):
                continue  # same impossible values we already pushed back on
            reason = check_triplet(rule.triplet, answers)
            if reason is not None:
                self._numeric_snapshots[rule.rule_key] = snapshot
                return ReAsk(rule_key=rule.rule_key, reason=reason, clarify=rule.clarify)
        return None
```

Also extend the module docstring's rule-kind list with one line (keep its style):

```
* `numeric_consistencies` re-arm the same way, but their `when` is computed —
  the money-triplet checks in vera_core.forms.consistency — and their ReAsk
  reason embeds the actual recorded amounts.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest apps/agent_worker/tests/unit/test_rule_engine.py tests/unit/forms/ -v`
Expected: all PASS (old rule-engine tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/call_plan.py apps/agent_worker/src/agent_worker/rule_engine.py apps/agent_worker/tests/unit/test_rule_engine.py
git commit -m "feat(agent): evaluate NumericConsistency rules in the rule engine"
```

---

### Task 4: Author the catalog rule + recompile the artifact

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py` (dsl import block :26-48; `contradictions=[...]` list ends at :1362, followed by the closing `)` of `FormSchemaDoc(...)` at :1363)
- Regenerate: `vera-backend/data/form_schemas/ibv_form_standard_v2.json` (via `just compile-schemas` — never by hand)
- Test: `vera-backend/tests/unit/forms/test_call_plan.py`

**Interfaces:**
- Consumes: `NumericConsistency` (Task 2); `compile_call_plan` carry (Task 3).
- Produces: the shipped ibv schema declares rule_key `lifetime_maximum_triplet_consistency` with `triplet="sections.lifetime_maximum"`; the compiled artifact (which the frontend tests import directly) now contains a top-level `numeric_consistencies` array.

- [ ] **Step 1: Write the failing test**

Add to `test_call_plan.py` (it already loads the real artifact as `IBV` and compiles `PLAN` at module scope):

```python
def test_plan_carries_numeric_consistencies() -> None:
    assert [r.rule_key for r in PLAN.numeric_consistencies] == [
        "lifetime_maximum_triplet_consistency"
    ]
    assert PLAN.numeric_consistencies[0].triplet == "sections.lifetime_maximum"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/forms/test_call_plan.py::test_plan_carries_numeric_consistencies -v`
Expected: FAIL — `PLAN.numeric_consistencies` is `[]` (artifact doesn't declare the rule yet).

- [ ] **Step 3: Author the rule in the catalog**

In `ibv_standard.py`, add `NumericConsistency` to the `vera_core.forms.dsl` import block (alphabetically, between `Leaf` and `PromotedFields`). Then, after the closing `]` of the `contradictions=[...]` list (line 1362), add the sibling argument:

```python
        numeric_consistencies=[
            NumericConsistency(
                rule_key="lifetime_maximum_triplet_consistency",
                triplet="sections.lifetime_maximum",
                clarify=(
                    "Could you double-check the total lifetime maximum for infertility "
                    "services, how much of it has been met, and how much remains?"
                ),
            ),
        ],
```

- [ ] **Step 4: Recompile the artifact**

Run: `just compile-schemas`
Expected: `ibv_form_standard_v2.json` gains the `numeric_consistencies` array (git diff shows only that addition); `disease_only` artifact unchanged.

- [ ] **Step 5: Run the forms suite (freshness + round-trip + new carry test)**

Run: `uv run pytest tests/unit/forms/ -v`
Expected: all PASS, including `test_committed_artifact_is_fresh` for both schemas.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py data/form_schemas/ibv_form_standard_v2.json tests/unit/forms/test_call_plan.py
git commit -m "feat(ibv): lifetime maximum triplet consistency rule"
```

---

### Task 5: Frontend review-UI validation pass

**Files:**
- Modify: `vera-frontend/src/lib/ibv/types.ts` (Contradiction type at :102-108; FormSchema at :110-121)
- Modify: `vera-frontend/src/lib/ibv/validation.ts` (imports at :1-2; `parseNumeric` at :10-12; `validateAll` at :122-130)
- Test: `vera-frontend/src/lib/ibv/validation.test.ts` (imports the real compiled artifact, which now has the rule after Task 4)

**Interfaces:**
- Consumes: the compiled artifact's `numeric_consistencies` (Task 4); existing `parseNumeric`, `leafByPath` (exported from `./schema`), `ValidationErrors`.
- Produces: `NumericConsistency` type; `validateAll` (and therefore `validateSection`) reports cross-field errors on each violated triplet path. Per-leaf errors are never overwritten (cross-field messages fill only empty slots). `validateCreate` is deliberately untouched (lifetime max is not intake data).

- [ ] **Step 1: Write the failing tests**

Add to `validation.test.ts`:

```ts
describe("validateAll — numeric consistency (lifetime maximum triplet)", () => {
  const TOTAL = "sections.lifetime_maximum.total"
  const MET = "sections.lifetime_maximum.met_amount"
  const REMAINING = "sections.lifetime_maximum.remaining"

  it("flags the bug-report example on every participating field", () => {
    const errors = validateAll(schema, { [TOTAL]: "$100", [MET]: "$300", [REMAINING]: "$300" })
    for (const path of [TOTAL, MET, REMAINING]) {
      expect(errors[path]).toMatch(/exceeds/i)
    }
    expect(errors[MET]).toContain("$300.00")
    expect(errors[MET]).toContain("$100.00")
  })

  it("flags a sum mismatch when nothing exceeds the total", () => {
    const errors = validateAll(schema, {
      [TOTAL]: "$25,000",
      [MET]: "$5,000",
      [REMAINING]: "$25,000",
    })
    expect(errors[TOTAL]).toMatch(/must equal/i)
  })

  it("accepts a consistent triplet and tolerates one-cent rounding", () => {
    const ok = validateAll(schema, { [TOTAL]: "$25,000", [MET]: "$5,000", [REMAINING]: "$20,000" })
    expect(ok[TOTAL]).toBeUndefined()
    const cent = validateAll(schema, { [TOTAL]: "100", [MET]: "50", [REMAINING]: "50.01" })
    expect(cent[TOTAL]).toBeUndefined()
  })

  it("stays silent on special values and partial data", () => {
    expect(validateAll(schema, { [TOTAL]: "No Limit" })[TOTAL]).toBeUndefined()
    const partial = validateAll(schema, { [TOTAL]: "$100", [MET]: "$50" })
    expect(partial[TOTAL]).toBeUndefined()
    expect(partial[MET]).toBeUndefined()
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from `vera-frontend/`): `npx vitest run src/lib/ibv/validation.test.ts`
Expected: the new describe block FAILS (no cross-field errors produced); pre-existing tests PASS.

- [ ] **Step 3: Implement types + validation pass**

`types.ts` — after the `Contradiction` type (line 108):

```ts
/** Money-triplet consistency rule over `<triplet>.total|met_amount|remaining`. */
export type NumericConsistency = {
  rule_key: string
  triplet: string
  clarify?: string
}
```

and in `FormSchema` after `contradictions?: Contradiction[]`:

```ts
  numeric_consistencies?: NumericConsistency[]
```

`validation.ts` — extend the schema import to include `leafByPath`:

```ts
import { allLeaves, createRequiredPaths, isApplicable, isRequired, leafByPath } from "./schema"
```

Add below `parseNumeric`:

```ts
function formatMoney(n: number): string {
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/**
 * Cross-field money-triplet checks (`numeric_consistencies`): met/remaining must
 * not exceed total and met + remaining must equal total (±$0.01, compared in
 * whole cents). Mirrors vera_core.forms.consistency — keep semantics in sync.
 */
function numericConsistencyErrors(schema: FormSchema, values: FormValues): ValidationErrors {
  const errors: ValidationErrors = {}
  const leaves = leafByPath(schema)
  for (const rule of schema.numeric_consistencies ?? []) {
    const totalPath = `${rule.triplet}.total`
    const metPath = `${rule.triplet}.met_amount`
    const remainingPath = `${rule.triplet}.remaining`
    const parse = (path: string): number | undefined => {
      const raw = (values[path] ?? "").trim()
      if (raw === "") return undefined
      const n = parseNumeric(raw)
      return Number.isNaN(n) ? undefined : n
    }
    const title = (path: string): string => leaves.get(path)?.field.title ?? path
    const total = parse(totalPath)
    const met = parse(metPath)
    const remaining = parse(remainingPath)

    const clauses: string[] = []
    const flagged = new Set<string>()
    if (total !== undefined && met !== undefined && met > total) {
      clauses.push(
        `${title(metPath)} (${formatMoney(met)}) exceeds ${title(totalPath)} (${formatMoney(total)})`
      )
      flagged.add(metPath).add(totalPath)
    }
    if (total !== undefined && remaining !== undefined && remaining > total) {
      clauses.push(
        `${title(remainingPath)} (${formatMoney(remaining)}) exceeds ${title(totalPath)} (${formatMoney(total)})`
      )
      flagged.add(remainingPath).add(totalPath)
    }
    if (
      clauses.length === 0 &&
      total !== undefined &&
      met !== undefined &&
      remaining !== undefined &&
      Math.abs(Math.round((met + remaining - total) * 100)) > 1
    ) {
      clauses.push(
        `${title(metPath)} (${formatMoney(met)}) plus ${title(remainingPath)} ` +
          `(${formatMoney(remaining)}) must equal ${title(totalPath)} (${formatMoney(total)})`
      )
      flagged.add(totalPath).add(metPath).add(remainingPath)
    }
    if (clauses.length > 0) {
      const message = clauses.join("; ")
      for (const path of flagged) errors[path] ??= message
    }
  }
  return errors
}
```

In `validateAll`, before its `return errors`:

```ts
  for (const [path, message] of Object.entries(numericConsistencyErrors(schema, values))) {
    errors[path] ??= message
  }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx vitest run src/lib/ibv/validation.test.ts`
Expected: all PASS (new and pre-existing).

- [ ] **Step 5: Commit**

```bash
git add src/lib/ibv/types.ts src/lib/ibv/validation.ts src/lib/ibv/validation.test.ts
git commit -m "feat(review): flag inconsistent money triplets in form validation"
```

---

### Task 6: Full verification and wrap-up

**Files:** none new — gates over the whole branch.

- [ ] **Step 1: Backend gate**

Run (from `vera-backend/`): `just check`
Expected: ruff check, ruff format --check, mypy --strict, pytest — all green. Fix anything red before proceeding (mypy strict commonly catches missing `-> None` on tests).

- [ ] **Step 2: Frontend gate — all four**

Run (from `vera-frontend/`): `npx tsc -b && npx eslint . && npm test && npm run build`
Expected: all four green.

- [ ] **Step 3: Simplify pass (mandatory per repo CLAUDE.md)**

Invoke the `code-simplifier` agent on the branch's changes ("simplify code"). After it applies refinements, **re-run Step 1 and Step 2 in full**.

- [ ] **Step 4: Local seed (optional, needs docker stack)**

If the local stack is up (`just up`, `just migrate`): run `just seed-schemas` and confirm it publishes a new ibv schema_version (order-sensitive equality → the changed document republishes). Existing forms pinned to the old version are unaffected by design.

- [ ] **Step 5: Voice-path verification note (manual gates before shipping)**

Rule-engine changes are an eval-harness trigger per repo rules. Flag to the user for pre-merge/pre-ship:
- Eval harness (needs Vertex ADC + seeded Postgres): `VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs` — ideally with a scenario where the rep quotes Total $100 / Met $300 / Remaining $300 and the agent pushes back.
- A live call before shipping (repo rule: a green eval run alone does not verify the voice path).

- [ ] **Step 6: Push and open PR**

```bash
git push origin HEAD:refs/heads/fix/lifetime-max-consistency
```

Open the PR from the URL Bitbucket prints (base: `dev`; `gh` does not work — Bitbucket remote).
