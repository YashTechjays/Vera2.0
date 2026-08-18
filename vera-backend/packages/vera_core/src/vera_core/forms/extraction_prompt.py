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


# Same exposure as the rule above — nothing normalizes a date on the AI write path either.
# UNGATED at ~215 chars/pass, deliberately: the field this was written for
# (`plan_year_information`) is a `text` leaf holding a RANGE, so no `date_format` reaches it
# and a `type == "date"` gate would never fire on it — and `build_extract_prompt` sees only
# bare paths, so gating one extractor and not the other IS the two-shapes defect above.
#
# Padded, and NOT `DATE_VALIDATION.date_format` ("M/D/YYYY", which `format_date` renders
# un-padded): the shape this column converges on is that leaf's padded derive literal, and
# padded input still parses under M/D/YYYY. Every clause stays subordinate to "a date answer"
# — generalized, it would truncate `additional_notes`, a leaf whose answer IS prose.
ANSWER_DATE_FORMAT_RULE = (
    'Write a date answer as digits in MM/DD/YYYY and nothing else — never "January 1st" '
    'and never the sentence around it; a date range is two such dates joined by " - ". '
    "Never add a year the representative did not state."
)


# "ADDITIONAL possibilities, not the full range" is load-bearing: read as the field's WHOLE
# vocabulary, the model treats a normal answer as not belonging and diverts it to a neighbouring
# free-text leaf. Said once here rather than per-field, which cost +18% of the whole prompt.
EXACT_VALUE_RULE = (
    'Some fields list a few named answers after "or exactly:". Those are ADDITIONAL '
    "possibilities, not the full range — answer normally in every other case, and only when "
    "the answer really is one of them, write it exactly as shown."
)


# Every ungated shape convention, in preamble order. Both extractors interpolate this block
# rather than naming the rules themselves, so a new convention lands on BOTH sides in one edit
# — the module's whole premise, previously upheld only by remembering to touch two files (which
# also spelled the separating space in two different places).
UNGATED_ANSWER_SHAPE_RULES = (ANSWER_UNIT_FORMAT_RULE, ANSWER_DATE_FORMAT_RULE)


def answer_shape_rules(*, names_exact: bool) -> str:
    """The shape rules a preamble carries; `names_exact` appends the one gated rule."""
    rules = [*UNGATED_ANSWER_SHAPE_RULES]
    if names_exact:
        rules.append(EXACT_VALUE_RULE)
    return " ".join(rules)


def special_values_hint(special_values: Sequence[str] | None) -> str:
    """The clause naming a NON-enum leaf's declared answers, or "" when it has none."""
    # Not optional on a currency leaf: without it the model writes no non-numeric answer at all.
    # Kept out of the enum `(one of: …)` clause because here they are ALTERNATIVES to a normal
    # answer, and "one of" pushes the model to pick a sentinel over the figure it was told.
    if not special_values:
        return ""
    return f" (or exactly: {', '.join(special_values)})"
