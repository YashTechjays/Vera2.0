"""Shared turn vocabulary + the finalized-turn model for the live call stream.

`role` says WHAT the turn is (speech vs. a keypad press vs. future event kinds) and is
meant to grow; `source` says WHO acted (the constrained actor set mirroring the
transcript table's `source` column) and drives attribution — e.g. which side of the
UI a turn renders on.

The Redis transport lives in `vera_core.call_stream` (`vera:call-events:{room}`), the
single live stream: it carries these turns as typed envelopes alongside call_status
frames, and feeds both SSE endpoints, the transcript finalizer, the summariser and the
worker's Observer. This module no longer owns a store — it is the vocabulary all of
them share.
"""

from typing import Any, Literal

from pydantic import BaseModel, model_validator

# Turn vocabulary, shared by every live stream (voice-lab transcript + real-call events).
# `role` says WHAT the turn is (speech vs. a keypad press vs. future event kinds) and is
# meant to grow; `source` says WHO acted (the constrained actor set mirroring the
# transcript table's `source` column) and drives attribution — e.g. which side of the
# UI a turn renders on.
ROLE_USER: Literal["user"] = "user"
ROLE_AGENT: Literal["agent"] = "agent"
ROLE_DTMF: Literal["dtmf"] = "dtmf"  # a keypad press (DTMF), text = the digits sent

SOURCE_REP: Literal["rep"] = "rep"  # the human on the line (payer rep / IVR side)
SOURCE_BOT: Literal["bot"] = "bot"  # Vera — speech or an action it took
SOURCE_SUPERVISOR: Literal["supervisor"] = "supervisor"  # a supervisor who took over the call

type TurnRole = Literal["user", "agent", "dtmf"]
type TurnSource = Literal["rep", "bot", "supervisor"]

_SOURCE_BY_ROLE: dict[str, TurnSource] = {
    ROLE_USER: SOURCE_REP,
    ROLE_AGENT: SOURCE_BOT,
    ROLE_DTMF: SOURCE_BOT,
}


def source_for_role(role: TurnRole) -> TurnSource:
    """The acting source implied by a role — the producer-side stamp for today's roles,
    and the consumer-side fallback for legacy stream entries published before `source`."""
    return _SOURCE_BY_ROLE[role]


# Mirrors the `transcript.source` CHECK enum (`models.enums.TranscriptSource`) without
# importing the ORM into this dependency-free vocabulary; the two sets are pinned equal by
# tests/unit/test_evidence_seq_parity.py.
_VALID_SOURCES: frozenset[str] = frozenset({SOURCE_REP, SOURCE_BOT, SOURCE_SUPERVISOR})


def resolve_turn_source(data: dict[str, Any]) -> str | None:
    """The source of a raw transcript envelope's `data`, or None when it can't be established.

    The one implementation shared by every consumer that numbers turns — the persistence
    finalizer (`transcript.seq`) and the worker's Observer (`evidence_seq`). They MUST skip
    exactly the same turns or `evidence_seq` mispoints into the persisted transcript, which
    is why this lives here rather than being written twice.

    The producer-stamped `source` is authoritative (validated against the constrained set).
    The role map is the fallback for legacy envelopes published before `source` existed:
    "user" is the payer rep, the human on a real call; "agent" is Vera's speech; "dtmf" is a
    keypad press Vera sent. A turn that resolves neither way can only come from a corrupted
    envelope, so callers drop it rather than guess (mapping it to BOT would misattribute
    speech that may not be the agent's).
    """
    stamped = data.get("source")
    if isinstance(stamped, str) and stamped in _VALID_SOURCES:
        return stamped
    return _SOURCE_BY_ROLE.get(str(data.get("role", "")))


class TranscriptEvent(BaseModel):
    """One finalized turn. `text` is always tokenized / de-identified."""

    role: TurnRole
    source: TurnSource
    text: str
    ts: int  # epoch milliseconds

    @model_validator(mode="before")
    @classmethod
    def _derive_source(cls, data: Any) -> Any:
        # Legacy stream entries (published before `source` existed) carry only a role;
        # derive the actor so no consumer ever sees a source-less turn mid-deploy.
        if isinstance(data, dict) and data.get("source") is None:
            role = data.get("role")
            if isinstance(role, str) and role in _SOURCE_BY_ROLE:
                return {**data, "source": _SOURCE_BY_ROLE[role]}
        return data
