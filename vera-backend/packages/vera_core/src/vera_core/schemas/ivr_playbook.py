"""Per-provider IVR playbook overlay — the `ivr_playbook.instructions` contract.

Admin-authored, non-PHI navigation config shared by the control plane (validate on
write, resolve the active playbook, serialize into dispatch metadata) and the agent
worker (parse at call start, template the generic navigator's <config> block). Every
field is a navigation hint (never a patient identifier), so the whole object is safe
to serialize into LiveKit dispatch metadata. The empty model is the no-op overlay:
each unset field falls back to the generic prompt's built-in default.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class IvrPlaybookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Provider-specific ID/menu sub-flows (e.g. Cigna ID-letter flow).
    provider_subflows: str | None = Field(default=None, max_length=1000)
    # Free-text provider-specific rules appended after the base navigator prompt. This is the
    # catch-all overlay: anything a provider needs to override in the generic navigator's
    # defaults (reach-a-human keyword, survey answer, date scope, …) is expressed here as prose.
    extra_rules: str | None = Field(default=None, max_length=4000)

    @classmethod
    def from_stored(cls, data: Mapping[str, Any]) -> "IvrPlaybookConfig":
        """Lenient read of a persisted `instructions` blob. The write path validates
        strictly (extra="forbid"), but the table predates it (seed scripts, raw SQL) and a
        future field rename can strand a row, so a stored value may carry unknown keys or a
        bad per-field value. This drops both — never raising — so the same tolerant overlay
        is what BOTH the admin read path (_detail) and runtime selection see, keeping display
        and behaviour in agreement. Worst case (nothing survives) is the empty no-op overlay,
        i.e. the generic navigator."""
        known = {k: v for k, v in data.items() if k in cls.model_fields}
        while known:
            try:
                return cls.model_validate(known)
            except ValidationError as exc:
                # Drop each field the validator rejected, then retry with the rest. `loc[0]`
                # is the offending field name (a str for these flat fields).
                bad = {str(loc[0]) for e in exc.errors() if (loc := e["loc"])} & known.keys()
                if not bad:
                    break
                for key in bad:
                    known.pop(key, None)
        return cls()
