"""How a simulated call is recorded and rendered as a transcript.

Its own module because the rendering is load-bearing twice over: the evaluator LLM reads it and
cites line numbers from it, and a human debugging a run reads the same lines. Keeping it out of
the marked eval module also lets it be tested in `just check` — no LLM, no database.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class TurnEvent:
    """One thing VERA did, kept in the order it actually happened."""

    kind: Literal["vera", "tool", "handoff"]
    text: str = ""  # what VERA said, or the tool name
    handoff: tuple[str, str] | None = None  # set only when kind == "handoff"
    reason: str = ""  # the model's stated why, for kind == "tool"

    def render(self) -> str:
        if self.handoff is not None:
            return ">>>> HANDOFF {} -> {}".format(*self.handoff)
        speaker = "VERA" if self.kind == "vera" else "TOOL"
        why = f" ({self.reason})" if self.reason else ""
        return f"{speaker} : {self.text}{why}"


@dataclass
class Turn:
    """One exchange: what the rep said, and what VERA did with it.

    `events` is ordered, and the three views below are projections of it. Binning the events into
    separate lists as they arrived is what used to destroy the interleaving: a turn that asked a
    question and THEN completed the task rendered identically to one that did the two in the
    other order, so a defect and correct conduct read the same to the judge."""

    rep: str
    events: list[TurnEvent] = field(default_factory=list)

    @property
    def vera(self) -> list[str]:
        return [e.text for e in self.events if e.kind == "vera"]

    @property
    def tools(self) -> list[str]:
        return [e.text for e in self.events if e.kind == "tool"]

    @property
    def handoffs(self) -> list[tuple[str, str]]:
        return [e.handoff for e in self.events if e.handoff is not None]

    def lines(self) -> list[str]:
        """The turn as transcript lines — the rep's words, then what VERA did, in order."""
        return [f"REP  : {self.rep}", *(e.render() for e in self.events)]


def _reason_of(arguments: Any) -> str:
    """The model's stated reason for a tool call, from the call item's JSON arguments.

    Every tool takes a required `reason`, but a transcript is worth more than a stack trace:
    a malformed or missing one degrades to an unexplained TOOL line rather than losing the run."""
    try:
        parsed = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("reason") or "").strip()


def _as_event(ev: Any) -> TurnEvent | None:
    """One `RunResult` event as a TurnEvent, or None for the kinds we do not transcribe."""
    if ev.type == "message" and ev.item.role == "assistant":
        return TurnEvent("vera", ev.item.text_content or "")
    if ev.type == "function_call":
        reason = _reason_of(getattr(ev.item, "arguments", None))
        return TurnEvent("tool", ev.item.name, reason=reason)
    if ev.type == "agent_handoff":
        names = (type(ev.old_agent).__name__, type(ev.new_agent).__name__)
        return TurnEvent("handoff", handoff=names)
    return None


def collect(rep_said: str, result: Any) -> Turn:
    """Fold one `RunResult` into a readable Turn, preserving event order."""
    events = (_as_event(ev) for ev in result.events)
    return Turn(rep=rep_said, events=[e for e in events if e is not None])


def echo(phase: str, turn: Turn) -> None:
    """Live transcript, visible under `pytest -s`. flush=True so a piped run still streams —
    without it the call buffers and a stall looks identical to slow progress."""
    for line in turn.lines():
        print(f"[{phase}] {line}", flush=True)
