"""Shared instruction text for the answer extractors.

Two separately deployed extractors read the same transcript and write into the same
`field_answer` column: the Observer's live pass (`agent_worker.observer`) and the post-call
top-up (`control_plane.llm`). Anything that must hold for BOTH belongs here — a second copy
of the rule is exactly how the two of them come to write two different shapes into one
column, which is the defect this text exists to prevent. Same pattern as
`vera_core.call_health.HEALTH_SYSTEM_PROMPT`: prompt text in `vera_core`, consumed by an app.
"""

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
