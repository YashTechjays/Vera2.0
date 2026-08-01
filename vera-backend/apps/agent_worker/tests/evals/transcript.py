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

    kind: Literal["vera", "tool", "result", "handoff", "ended"]
    text: str = ""  # what VERA said, the tool name, or what the tool answered
    handoff: tuple[str, str] | None = None  # set only when kind == "handoff"
    reason: str = ""  # the model's stated why, for kind == "tool"
    call_id: str = ""  # livekit's id for a tool call; never rendered, only deduped on

    def render(self) -> str:
        if self.handoff is not None:
            return ">>>> HANDOFF {} -> {}".format(*self.handoff)
        if self.kind == "ended":
            return f">>>> CALL ENDED ({self.text}) — {self.reason}"
        if self.kind == "result":
            return f"  <- {self.text}"
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
    def is_tail(self) -> bool:
        """The closing block, which nobody said — it belongs to no exchange (see `tail`)."""
        return self.rep == ""

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
        said = [] if self.is_tail else [f"REP  : {self.rep}"]
        return [*said, *(e.render() for e in self.events)]


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
        return TurnEvent("tool", ev.item.name, reason=reason, call_id=ev.item.call_id)
    if ev.type == "function_call_output":
        # A tool that answers in words REFUSED or redirected — the premature-completion guard
        # and the takeover latch both work that way, and an unrecorded refusal reads as a
        # handoff that simply never happened.
        if not ev.item.output:
            return None
        return TurnEvent("result", str(ev.item.output).strip())
    if ev.type == "agent_handoff":
        names = (type(ev.old_agent).__name__, type(ev.new_agent).__name__)
        return TurnEvent("handoff", handoff=names)
    return None


def collect(rep_said: str, result: Any) -> Turn:
    """Fold one `RunResult` into a readable Turn, preserving event order."""
    events = (_as_event(ev) for ev in result.events)
    return Turn(rep=rep_said, events=[e for e in events if e is not None])


def tail(turns: list[Turn], calls: list[Any], closed: str, *, hung_up_in_code: bool) -> Turn | None:
    """The closing block: whatever the driven turns missed, then how the call ended.

    A `RunResult` only records while its own future is open (`run_result.py:155`, `:172`), so the
    wrap-up agent's tool call — made out of band from `on_enter`, after the last driven turn —
    lands in no run window at all. `calls` comes from the session-scoped
    `function_tools_executed` event instead, which fires either way; anything already transcribed
    is dropped here by `call_id`.

    `closed` is the livekit CloseReason, or "" if the session never closed — in which case there
    is no ending to report and a run that merely stopped must not read as a clean hangup."""
    if not closed:
        return None
    seen = {e.call_id for turn in turns for e in turn.events if e.call_id}
    events = [
        TurnEvent("tool", call.name, reason=_reason_of(call.arguments), call_id=call.call_id)
        for call in calls
        if call.call_id not in seen
    ]
    if closed != "user_initiated":
        # The harness's `async with` closes the session on exit, so a call that never hung up
        # itself still reports a close — saying so beats implying the agent ended it.
        why = "the harness closed the session; the call did not end itself"
    elif hung_up_in_code:
        why = "hung up in code after the closing outro; no end_call"
    else:
        why = "the model ended the call"
    return Turn(rep="", events=[*events, TurnEvent("ended", closed, reason=why)])


def echo(phase: str, turn: Turn) -> None:
    """Live transcript, visible under `pytest -s`. flush=True so a piped run still streams —
    without it the call buffers and a stall looks identical to slow progress."""
    for line in turn.lines():
        print(f"[{phase}] {line}", flush=True)
