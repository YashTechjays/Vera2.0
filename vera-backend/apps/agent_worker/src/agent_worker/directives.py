"""Rule-engine directives — what the rule engine asks the live conversation to do.

The Observer's rule engine produces one of these after an answer is recorded, and the
Observer hands it to `PlanRunController.apply_directive_now`, which interrupts the bot
(it goes silent) and then swaps the agent (Terminate / SkipToTask) or re-asks (ReAsk).
Serialized on the controller lock against an in-flight `task_complete` handoff.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Terminate:
    """End the call now: swap the live agent to the wrap-up agent."""

    rule_key: str


@dataclass(frozen=True, slots=True)
class SkipToTask:
    """Jump the conversation to a specific task, skipping whatever lies between."""

    rule_key: str
    task_key: str


@dataclass(frozen=True, slots=True)
class ReAsk:
    """Push back once and re-clarify: the same agent re-asks in the current turn
    (no agent swap), driven by an injected consistency-check instruction."""

    rule_key: str
    reason: str
    clarify: str | None = None
    fields: tuple[str, ...] = field(default_factory=tuple)


type Directive = Terminate | SkipToTask | ReAsk
