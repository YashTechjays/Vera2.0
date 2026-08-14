"""Shared instruction text for the answer extractors.

Two separately deployed extractors read the same transcript and write into the same
`field_answer` column: the Observer's live pass (`agent_worker.observer`) and the post-call
top-up (`control_plane.llm`). Anything that must hold for BOTH belongs here — a second copy
of the rule is exactly how the two of them come to write two different shapes into one
column, which is the defect this text exists to prevent. Same pattern as
`vera_core.call_health.HEALTH_SYSTEM_PROMPT`: prompt text in `vera_core`, consumed by an app.
"""

from collections.abc import Sequence

# `percent`-typed leaves (coinsurance) were stored inconsistently — a bare "20" from the
# extractor sitting beside the "0%" the either/or auto-fill writes from the leaf's authored
# `inapplicable_value`. Storage IS display here (the review UI and the xlsx export render the
# stored string verbatim), so one CPT matrix column could show both.
#
# This instruction is the ONLY thing keeping the shape consistent — nothing normalizes on
# write and no backfill has run, so a model that drifts lands straight in the customer-facing
# export. Treat it accordingly when editing.
#
# Percent only, deliberately: `currency` leaves have the same defect, but telling the model to
# switch money to "$20" would change its stored shape with nothing to converge it — worse than
# leaving currency alone. Extend this only alongside a currency fix.
#
# No range hint either ("0-100"): it nudges the model into rescaling a fraction like 0.2 into
# 20%, and 0.2 can legitimately mean 0.2%.
ANSWER_UNIT_FORMAT_RULE = (
    'Write a percentage answer as digits with the sign and nothing else — "20%", never '
    '"20" or "twenty percent"; "0%" for none.'
)


# Says once what a per-field clause would otherwise repeat on all 72 annotated fields — the
# wrapper, not the literals, is the cost: spelling the rule inline came to +18% on the whole
# prompt and +22% on the CPT panel, re-sent on every extraction pass with no caching.
EXACT_VALUE_RULE = (
    'Where a field lists values after "exact:", and the answer is one of them, write it '
    "exactly as shown there."
)


def special_values_hint(special_values: Sequence[str] | None) -> str:
    """The clause naming a NON-enum leaf's declared answers, or "" when it has none.

    Kept out of the enum `(one of: …)` clause because these are ALTERNATIVES to a normal
    answer, not the whole vocabulary — a deductible is usually an amount, and "one of" would
    push the model to pick a sentinel over the figure it was told. An enum has no such
    figure, so its caller folds both lists into the one clause instead (as `intake`'s
    `enum_accepted_values` already does)."""
    if not special_values:
        return ""
    return f" (exact: {', '.join(special_values)})"
