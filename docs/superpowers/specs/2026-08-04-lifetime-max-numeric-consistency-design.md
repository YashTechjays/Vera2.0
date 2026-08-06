# Lifetime Maximum numeric-consistency validation — design

**Date:** 2026-08-04
**Status:** Approved
**Scope decision:** Lifetime Maximum triplet only (`sections.lifetime_maximum`); the
mechanism is built to extend to the deductible/OOP triplets later by authoring more rules.

## Problem

The voice agent accepts logically impossible money-triplet values without pushback.
Example: Lifetime Maximum Total = $100, Met Amount = $300, Remaining = $300. Met or
Remaining can never exceed Total, and Met + Remaining must equal Total. Today nothing
in the live-call write path (`ObserverManager._record_locked` → `record_answer`) or the
review UI checks cross-field numeric consistency, so impossible values are stored and
displayed as trustworthy.

## Decisions (from brainstorming)

1. **Scope:** `sections.lifetime_maximum` only.
2. **Checks (full consistency):** with T/M/R parsed as currency —
   - Met > Total → violation
   - Remaining > Total → violation
   - |Met + Remaining − Total| > $0.01 → violation
   A check runs only when all of its operands parse as numbers; special values
   ("No Limit", "Unlimited", "Met") and unparseable strings simply don't participate.
3. **Storage behavior:** store, flag, re-ask. Values are always recorded; the agent
   immediately re-asks via the existing `ReAsk` directive; corrected answers overwrite
   through the normal observer path. If the call ends unresolved, the review UI shows a
   validation error on the triplet — nothing is silently trusted, nothing is lost.
4. **Approach:** purpose-built triplet rule (new small DSL rule type consumed by the
   rule engine), NOT a generic arithmetic extension of the `Comparison` condition DSL.

## Architecture

One declaration in the form catalog drives both enforcement surfaces:

```
catalog (ibv_standard.py)
  └─ NumericConsistency(rule_key, triplet, clarify)     ← authored once
       ├─ compiled into schema doc → CallPlan → RuleEngine → ReAsk   (live call)
       └─ compiled into schema doc → review UI validateAll pass      (review flag)
```

### Backend

**1. DSL (`vera_core/forms/dsl.py`)**

```python
class NumericConsistency(_Model):
    """Money-triplet consistency rule: met/remaining must not exceed total
    and met + remaining must equal total."""
    rule_key: str
    triplet: str          # root-anchored base path, e.g. "sections.lifetime_maximum"
    clarify: str | None = None
```

- `FormSchemaDoc` gains `numeric_consistencies: list[NumericConsistency] | None = None`.
- Document validator: for each rule, `<triplet>.total`, `<triplet>.met_amount`,
  `<triplet>.remaining` must resolve to defined leaves of type `currency`;
  `rule_key` must be unique across flow rules / contradictions / numeric rules.
- Additive optional field — **no `dsl_version` bump** (old documents remain valid;
  round-trip identity holds).

**2. Evaluation helper (`vera_core/forms/consistency.py`, new module)**

- `parse_currency(value: str) -> float | None` — strip `$`, commas, whitespace;
  return `None` for anything that doesn't parse (special values, blanks, prose).
- `check_triplet(rule, answers) -> Violation | None` — applies the three checks over
  whichever operands are numeric. `Violation` carries a **dynamic reason** embedding
  the actual amounts, e.g. *"Lifetime maximum values are inconsistent: met ($300.00)
  plus remaining ($300.00) does not match the total ($100.00)."* — so the agent's
  pushback quotes the rep's own numbers.
- Pure and synchronous, no I/O; unit-testable in isolation.

**3. CallPlan (`vera_core/forms/call_plan.py`)**

- Carry `numeric_consistencies: list[NumericConsistency]` from the doc, exactly as
  `contradictions` is carried today.

**4. Rule engine (`agent_worker/rule_engine.py`)**

- After the `contradictions` loop, evaluate each `NumericConsistency`:
  - snapshot = the tuple of the three field values; skip if identical to the
    combination that last fired (same re-arm semantics as contradictions —
    challenged once per distinct combination, re-challenged on a new one).
  - on violation, return `ReAsk(rule_key, reason=violation.reason, clarify=rule.clarify)`.
- No changes to `directives.py` or `apply_directive_now` — the existing ReAsk path
  (interrupt + "CONSISTENCY CHECK: {reason} …") is reused unchanged.
- Ordering: flow rules → contradictions → numeric consistencies (a terminal redirect
  or an authored contradiction still wins the turn).

**5. Catalog (`vera_core/forms/catalog/ibv_standard.py`)**

One authored rule:

```python
numeric_consistencies=[
    NumericConsistency(
        rule_key="lifetime_maximum_triplet_consistency",
        triplet="sections.lifetime_maximum",
        clarify=(
            "Could you double-check the total lifetime maximum, how much of it "
            "has been met, and how much remains?"
        ),
    ),
],
```

Then `just compile-schemas` (freshness test enforces this) and `just seed-schemas`
(order-sensitive equality → publishes a new schema_version). Patient forms pinned to
the previous version are unaffected; new forms pick up the rule.

### Frontend (review UI — the "flag" half)

**`src/lib/ibv/types.ts` / `schema.ts`:** the schema type/parse learns the optional
`numeric_consistencies` array (rule_key, triplet, clarify — clarify unused by the UI).

**`src/lib/ibv/validation.ts`:** `validateAll` gains a cross-field pass after the
per-leaf loop: for each rule, parse `<triplet>.total|met_amount|remaining` with the
existing `parseNumeric` (skip non-numeric operands, mirroring the backend), run the
same three checks with the same $0.01 tolerance, and on violation attach one message
to **each participating path** (so the offending fields all render red), e.g.
`"Met Amount ($300.00) plus Remaining ($300.00) must equal Lifetime Maximum ($100.00)"`.
Because the triplet paths share the section prefix, `validateSection` picks these up
with no change.

Backend/frontend parity note: `consistency.py` ⇄ the `validation.ts` pass must stay
in sync (same operand-skipping and tolerance), in the same spirit as the documented
`conditions.py` ⇄ `conditions.ts` contract.

## Edge cases

- **Partial data:** checks needing an absent/non-numeric operand are skipped — no
  false ReAsk while the triplet is mid-collection (e.g. Met recorded before Total).
  The exceed checks need two operands; the sum check needs all three.
- **Special values:** Total = "No Limit"/"Unlimited" gates met/remaining off in the
  schema already; Remaining = "Met" doesn't parse → doesn't participate.
- **Rep restates the same bad numbers:** snapshot re-arm → no second challenge for
  the identical combination (matches contradiction behavior, accepted team semantics).
- **Cents/rounding:** $0.01 tolerance on the sum check.
- **Unresolved at call end:** values stay recorded with the review-UI error as the
  durable flag; no persistence/schema-storage change required.

## Out of scope

- Deductible and OOP triplets (author more rules later; zero code needed).
- Rendering the rule into the agent's task prompt (prompting.py) — the ReAsk directive
  is the enforcement; proactive prompt text can be added later if evals show value.
- Generic numeric operators in the `Comparison` condition DSL.
- Backend-side write blocking (rejected in brainstorming: order-dependent, loses data).

## Testing & verification

- **Unit (backend):** `consistency.py` (parse tolerance, specials, partials, each
  violation shape, dynamic reason text); rule-engine numeric evaluation + re-arm;
  DSL validator rejections (missing child, non-currency child, duplicate rule_key);
  compiled-schema freshness + round-trip.
- **Unit (frontend):** the new `validateAll` pass (violation, skip-on-special,
  partial data, error attached to all three paths).
- **Gates:** backend `just check`; frontend `tsc -b` + `eslint` + `npm test` + build.
- **Voice path:** eval-harness scenario where the rep quotes impossible lifetime-max
  values (rule-engine changes are an eval-harness trigger per repo rules), and a live
  call before shipping.
