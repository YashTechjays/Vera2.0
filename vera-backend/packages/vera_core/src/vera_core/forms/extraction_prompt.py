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
#
# "ADDITIONAL possibilities, not the full range" is doing the real work. An earlier wording
# ("values after `exact:`") read as the field's WHOLE vocabulary, and the model then treated a
# normal answer as not belonging: on call 01a00003-e418-78b3-8044-3d64086d1022 every numeric
# cycle limit was diverted into the neighbouring free-text `Additional Notes` and the leaf
# itself came back empty. Measured over the two transcripts that pull hardest in opposite
# directions — a sentinel on a currency leaf, a normal value on a text leaf beside a catch-all
# — that phrasing scored 3/3 and 0/3; this one scores 3/3 and 3/3. Re-run the pair before
# touching this text.
EXACT_VALUE_RULE = (
    'Some fields list a few named answers after "or exactly:". Those are ADDITIONAL '
    "possibilities, not the full range — answer normally in every other case, and only when "
    "the answer really is one of them, write it exactly as shown."
)


def special_values_hint(special_values: Sequence[str] | None) -> str:
    """The clause naming a NON-enum leaf's declared answers, or "" when it has none.

    Kept out of the enum `(one of: …)` clause because these are ALTERNATIVES to a normal
    answer, not the whole vocabulary — a deductible is usually an amount, and "one of" would
    push the model to pick a sentinel over the figure it was told. An enum has no such
    figure, so its caller folds both lists into the one clause instead (as `intake`'s
    `enum_accepted_values` already does).

    The clause is NOT optional on a currency leaf: without it the model declines to write a
    non-numeric answer at all, and "that one is unlimited" was dropped in 3 of 3 runs."""
    if not special_values:
        return ""
    return f" (or exactly: {', '.join(special_values)})"
