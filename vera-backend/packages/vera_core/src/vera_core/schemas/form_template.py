"""Data-driven form templates.

A tenant's eligibility/prior-auth questionnaire is data, not code: the agent
worker walks the template's fields during a call and the control plane stores
the answers. PHI-bearing fields are flagged so the boundary knows what gets
tokenized and how answers may be displayed.

TODO(vera-2.x): persistence (form_template table + versioning) and the data
extraction that fills these from call transcripts attach here.
"""

import enum

from pydantic import BaseModel, Field, field_validator, model_validator


class FieldType(enum.StrEnum):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    SELECT = "select"


class FormField(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    type: FieldType
    required: bool = False
    help_text: str = ""
    # SELECT only
    options: list[str] = Field(default_factory=list)
    # PHI flag: answers are tokenized at rest/in transit outside the vault.
    phi: bool = False
    # phi_codec EntityType name (e.g. "BENEFICIARY_ID") when phi=True.
    entity_type: str | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> "FormField":
        if self.type is FieldType.SELECT and not self.options:
            raise ValueError(f"select field '{self.key}' needs options")
        if self.type is not FieldType.SELECT and self.options:
            raise ValueError(f"non-select field '{self.key}' must not define options")
        if self.phi and not self.entity_type:
            raise ValueError(f"phi field '{self.key}' needs entity_type")
        return self


class FormTemplate(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    version: int = Field(ge=1)
    title: str
    description: str = ""
    fields: list[FormField] = Field(min_length=1)

    @field_validator("fields")
    @classmethod
    def _unique_keys(cls, fields: list[FormField]) -> list[FormField]:
        keys = [f.key for f in fields]
        if len(keys) != len(set(keys)):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            raise ValueError(f"duplicate field keys: {dupes}")
        return fields

    @property
    def phi_fields(self) -> list[FormField]:
        return [f for f in self.fields if f.phi]
