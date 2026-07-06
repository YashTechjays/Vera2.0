"""Call-plan contract — the compiled, per-call runtime artifact.

The control plane renders the v2 form-schema into a `CallPlan` (see
`callplan.render`) and stashes it in Redis (`callplan.store`); the agent worker
fetches it and runs `flat_instructions`. This module is the cross-process
contract, so the model is strict (`extra="forbid"`).

Raw prefilled values are carried inside `flat_instructions` (PHI tokenization was
removed as a dev simplification) — synthetic-data-only until a protection
mechanism is reintroduced (see adr/devops-todo.md #8). `composite` is the
schema-derived prompt document (from `forms.prompting.compile_prompt_document`),
carried for the later per-task-agent milestone.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class CallPlan(BaseModel):
    """The complete compiled artifact for one call. Serialized to Redis by the
    control plane; deserialized by the worker."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    room_name: str
    tenant_id: str
    call_id: str
    schema_version_id: str
    prompt_version_id: str | None = None
    greeting: str
    flat_instructions: str
    composite: dict[str, Any] = {}
