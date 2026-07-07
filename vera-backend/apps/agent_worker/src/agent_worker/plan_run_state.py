"""Shared, mutable state for one plan-driven call.

Constructed ONCE in the worker entrypoint and handed to every task agent, so the answer
map survives the LiveKit agent swap (the design's Seam 2). Read it at the top of every
agent, never re-create it per task.
"""

from dataclasses import dataclass, field

from vera_core.forms.planning import CallPlan
from vera_core.phi import PHIBoundaryProtocol


@dataclass
class PlanRunState:
    plan: CallPlan
    boundary: PHIBoundaryProtocol
    session_id: str
    # root-anchored field_path → recorded answer; the single source of truth for the call.
    answers: dict[str, str] = field(default_factory=dict)
