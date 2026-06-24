"""Tenant persona overlay — the `Tenant.persona_tweak` runtime knob contract.

Admin-authored, non-PHI configuration shared by the control plane (validate on
write, serialize into dispatch metadata) and the agent worker (parse at call
start). The empty model is the documented no-op default for the JSONB column.
"""

from pydantic import BaseModel, ConfigDict, Field


class PersonaTweak(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Appended to the base SYSTEM_PROMPT. Length-capped to bound prompt growth.
    extra_instructions: str | None = Field(default=None, max_length=4000)
    # Overrides the base outbound GREETING when set.
    greeting: str | None = Field(default=None, max_length=500)
