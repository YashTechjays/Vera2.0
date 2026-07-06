"""Call-plan runtime — render the v2 form-schema into a per-call agent prompt,
transport it to the worker, and hold the persona layer."""

from vera_core.callplan.model import CallPlan
from vera_core.callplan.persona import (
    BASE_PERSONA,
    CARTESIA_MARKUP_GUIDE,
    DEFAULT_GREETING,
)
from vera_core.callplan.prefill import build_prefill
from vera_core.callplan.render import render_runtime_prompt
from vera_core.callplan.store import CallPlanStore, call_plan_key

__all__ = [
    "BASE_PERSONA",
    "CARTESIA_MARKUP_GUIDE",
    "DEFAULT_GREETING",
    "CallPlan",
    "CallPlanStore",
    "build_prefill",
    "call_plan_key",
    "render_runtime_prompt",
]
