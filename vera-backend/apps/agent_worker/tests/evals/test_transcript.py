"""Deterministic tests for how a simulated call is recorded and rendered.

Deliberately NOT marked `evals`: no LLM and no database, so these run in `just check`. The
rendering is what the evaluator LLM reads and cites line numbers from, so an ordering bug here
silently mis-grades every run — which is exactly what happened before `Turn.events` existed.
"""

from dataclasses import dataclass, field
from typing import Any

from transcript import TurnEvent, collect


@dataclass
class _Item:
    role: str = "assistant"
    text_content: str | None = None
    name: str = ""


@dataclass
class _Event:
    """Stands in for a livekit RunResult event: `message`, `function_call` or `agent_handoff`."""

    type: str
    item: _Item = field(default_factory=_Item)
    old_agent: Any = None
    new_agent: Any = None


@dataclass
class _Result:
    events: list[_Event]


class _OldAgent: ...


class _NewAgent: ...


def _said(text: str) -> _Event:
    return _Event("message", _Item(text_content=text))


def _called(name: str) -> _Event:
    return _Event("function_call", _Item(name=name))


def _handed_off() -> _Event:
    return _Event("agent_handoff", old_agent=_OldAgent(), new_agent=_NewAgent())


class TestEventOrder:
    """The interleaving of speech, tool calls and handoffs within ONE turn is the evidence for
    several defects — asking a question and completing the task in the same turn, or handing off
    mid-answer. Binning events by kind as they arrive destroys exactly that evidence."""

    def test_order_is_preserved_across_kinds(self) -> None:
        turn = collect("Yes, it is covered.", _Result([_said("Thanks."), _called("task_complete")]))
        assert [e.kind for e in turn.events] == ["vera", "tool"]

    def test_a_tool_before_speech_renders_before_it(self) -> None:
        # The distinguishing case: same events, other order. Before `events`, both rendered with
        # the spoken line first, so completing-then-asking and asking-then-completing were
        # indistinguishable to the judge.
        turn = collect("Go ahead.", _Result([_called("task_complete"), _said("Next question?")]))
        assert turn.lines() == [
            "REP  : Go ahead.",
            "TOOL : task_complete",
            "VERA : Next question?",
        ]

    def test_the_three_views_still_project_the_events(self) -> None:
        turn = collect(
            "Sure.",
            _Result([_said("One moment."), _called("press_keypad"), _handed_off()]),
        )
        assert turn.vera == ["One moment."]
        assert turn.tools == ["press_keypad"]
        assert turn.handoffs == [("_OldAgent", "_NewAgent")]

    def test_a_handoff_renders_with_both_agent_names(self) -> None:
        turn = collect("Hello?", _Result([_handed_off()]))
        assert turn.lines()[-1] == ">>>> HANDOFF _OldAgent -> _NewAgent"

    def test_the_rep_always_speaks_first_in_a_turn(self) -> None:
        # The rep's words are what VERA is reacting to, so they must lead the turn regardless of
        # what VERA then did — including a turn where VERA said nothing at all.
        assert collect("Anything else?", _Result([])).lines() == ["REP  : Anything else?"]
        assert collect("Ok.", _Result([_called("end_call")])).lines()[0] == "REP  : Ok."

    def test_a_user_message_is_not_recorded_as_veras_speech(self) -> None:
        # `session.run` echoes the driving user turn back as an event; only assistant messages
        # are VERA speaking.
        result = _Result([_Event("message", _Item(role="user", text_content="the rep again"))])
        assert collect("the rep again", result).events == []

    def test_speech_with_no_text_is_kept_as_an_empty_line(self) -> None:
        # A turn VERA took but said nothing in is still evidence; dropping it would silently
        # renumber every later line the judge cites.
        turn = collect("Hi.", _Result([_Event("message", _Item(text_content=None))]))
        assert turn.events == [TurnEvent("vera", "")]


class TestUnknownEvents:
    def test_an_unrecognised_event_type_is_ignored(self) -> None:
        # livekit adds event types over time; an unknown one must not become a transcript line.
        turn = collect("Hi.", _Result([_Event("something_new"), _said("Hello.")]))
        assert turn.lines() == ["REP  : Hi.", "VERA : Hello."]
