"""Per-provider IVR playbook overlay — the `ivr_playbook.instructions` contract.

Admin-authored, non-PHI navigation config shared by the control plane (validate on
write, resolve the active playbook, serialize into dispatch metadata) and the agent
worker (parse at call start, template the generic navigator's <config> block). Every
field is a navigation hint (never a patient identifier), so the whole object is safe
to serialize into LiveKit dispatch metadata. The empty model is the no-op overlay:
each unset field falls back to the generic prompt's built-in default.
"""

from pydantic import BaseModel, ConfigDict, Field


class IvrPlaybookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # One way to exit announcement mode — the provider's first-prompt phrase.
    transition_trigger: str | None = Field(default=None, max_length=200)
    # The word that routes to a human (e.g. UHC's "Advocate" instead of "Representative").
    rep_keyword: str | None = Field(default=None, max_length=100)
    # Answer to an "other/multiple patients?" gate.
    multiple_patients_answer: str | None = Field(default=None, max_length=100)
    # Answer to a post-call survey offer (some providers expect "Yes").
    survey_answer: str | None = Field(default=None, max_length=100)
    # Answer to "as of today, or a past date?".
    date_scope: str | None = Field(default=None, max_length=100)
    # Answer to a "callback vs remain on hold" choice.
    callback_vs_hold: str | None = Field(default=None, max_length=100)
    # Provider-specific ID/menu sub-flows (e.g. Cigna ID-letter flow).
    provider_subflows: str | None = Field(default=None, max_length=1000)
    # Free-text provider-specific rules appended after the base navigator prompt.
    extra_rules: str | None = Field(default=None, max_length=4000)
