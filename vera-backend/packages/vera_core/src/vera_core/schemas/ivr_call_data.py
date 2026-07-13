"""Per-call patient/provider identifiers for the IVR navigator prompt.

Unlike `ivr_playbook` (admin-authored, non-PHI navigation hints), every field here is
PHI — the actual patient/provider identifiers the navigator SPEAKS or KEYS into the payer's
phone tree (member ID, DOB, name, group number, and the ordering provider's NPI/Tax ID).
The control plane reads them off the call's `patient_form` at dispatch and serializes them
into LiveKit dispatch metadata under `ivr_call_data`; the worker parses them and templates
them into the generic navigator prompt (see `agent_worker.ivr_prompt`).

NOTE — PHI wall deliberately bypassed. The repo's default posture keeps raw PHI out of the
LLM prompt (tokenize -> hydrate). That machinery is currently a no-op, and for this test
phase the values are templated straight into the prompt. Keep this confined to synthetic /
test data; these values must never reach a log / trace / span / URL.
"""

from pydantic import BaseModel, ConfigDict, Field


class IvrCallData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_name: str | None = Field(default=None, max_length=255)
    member_id: str | None = Field(default=None, max_length=128)
    # Spoken/keyed date form "MM/DD/YYYY" — the prompt's ID-entry rule derives both the
    # natural spoken form and the 8-digit keypad form from it, so it stays a single string.
    date_of_birth: str | None = Field(default=None, max_length=32)
    group_number: str | None = Field(default=None, max_length=128)
    provider_npi: str | None = Field(default=None, max_length=16)
    # No distinct "provider ID" source in the intake schema — reuses provider_npi today.
    provider_id: str | None = Field(default=None, max_length=32)
    tax_id: str | None = Field(default=None, max_length=16)
