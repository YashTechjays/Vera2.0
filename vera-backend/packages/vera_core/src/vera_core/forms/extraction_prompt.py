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


# "ADDITIONAL possibilities, not the full range" is load-bearing: read as the field's WHOLE
# vocabulary, the model treats a normal answer as not belonging and diverts it to a neighbouring
# free-text leaf. Said once here rather than per-field, which cost +18% of the whole prompt.
EXACT_VALUE_RULE = (
    'Some fields list a few named answers after "or exactly:". Those are ADDITIONAL '
    "possibilities, not the full range — answer normally in every other case, and only when "
    "the answer really is one of them, write it exactly as shown."
)


# Told only that the field is `(one of: Yes, No, N/A)`, both extractors wrote `Yes` from a rep
# saying "that code is valid" — a coverage claim the rep never made, which then retired the
# question from the owed set so the completion guard had nothing left to refuse.
COVERAGE_STATUS_RULE = (
    "A coverage-status field records the plan BENEFIT, not the code. Fill it only from an "
    "explicit statement that the service is or is not covered. A representative who says the "
    "code is valid, billable, active, recognized or on file has described the CODE and has "
    "not answered coverage — omit the field."
)


def is_coverage_status_path(path: str) -> bool:
    return path.endswith("covered")


def special_values_hint(special_values: Sequence[str] | None) -> str:
    """The clause naming a NON-enum leaf's declared answers, or "" when it has none."""
    # Not optional on a currency leaf: without it the model writes no non-numeric answer at all.
    # Kept out of the enum `(one of: …)` clause because here they are ALTERNATIVES to a normal
    # answer, and "one of" pushes the model to pick a sentinel over the figure it was told.
    if not special_values:
        return ""
    return f" (or exactly: {', '.join(special_values)})"
