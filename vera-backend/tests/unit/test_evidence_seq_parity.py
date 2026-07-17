"""Pin evidence_seq ↔ transcript.seq parity (the Observer↔finalizer contract).

The Observer counts entries of `vera:transcript:{room}` to stamp `evidence_seq`
(observer.py `_ingest`); the finalizer numbers `transcript.seq` over the rows it
persists from `vera:call-events:{room}` (`_build_rows`). Both streams are fed the
same ordered turns by the same fan-out emitter, but their skip rules differ:
non-transcript envelopes (call_status) exist only on the call-events side and
must not consume a seq slot there, while every turn — including dtmf — consumes a
slot on both sides. This test drives ONE turn sequence through both counters and
asserts each rep turn gets the identical seq, so a semantic drift in either
counter fails here instead of silently mispointing `field_answer.evidence_seq`.
"""

import uuid
from typing import Any

from agent_worker.observer import ObserverManager
from control_plane.transcript_finalizer import _build_rows
from vera_core.call_stream import TYPE_CALL_STATUS, TYPE_TRANSCRIPT, CallStreamEvent
from vera_core.forms.call_plan import CallPlan, PlanSession, PlanTask
from vera_core.observability.correlation import RoomRef
from vera_core.transcript import TranscriptEvent

ROOM = "call--t--c"

# One canonical call: (role, source, text). The dtmf turn matters — it occupies a
# seq slot in BOTH counters (the spec's "dtmf slots" hazard).
_TURNS: list[tuple[str, str, str]] = [
    ("agent", "bot", "What is the member ID?"),
    ("user", "rep", "It's ABC123."),
    ("dtmf", "bot", "1"),
    ("agent", "bot", "Is the deductible met?"),
    ("user", "rep", "Yes, fully met."),
]


class _RecordingObserver:
    """Stands in for the active TaskObserver; records the seq the manager stamps."""

    def __init__(self) -> None:
        self.turns: list[tuple[str, str, int]] = []  # (role, text, seq)

    def feed(self, turn: Any) -> None:
        self.turns.append((turn.role, turn.text, turn.seq))


class _Null:
    """Inert stand-in for the manager's collaborators (never touched by _ingest)."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - safety net
        raise AssertionError(f"unexpected collaborator access: {name}")


def _manager_with_recorder() -> tuple[ObserverManager, _RecordingObserver]:
    plan = CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[PlanTask(task_key="t1", title="T1", prompt=".")],
    )

    class _Controller:
        active_task_index = 0

    manager = ObserverManager(
        plan,
        controller=_Controller(),  # type: ignore[arg-type]
        run_state=_Null(),  # type: ignore[arg-type]
        bus=_Null(),  # type: ignore[arg-type]
        extractor=_Null(),  # satisfies the AnswerExtractor protocol structurally
        transcript=_Null(),  # satisfies the TranscriptSource protocol structurally
        room_name=ROOM,
    )
    recorder = _RecordingObserver()
    # Pin the active observer so _ingest routes every turn to the recorder without
    # rotating (active_task_index stays 0 == _active_index).
    manager._active_index = 0
    manager._active = recorder  # type: ignore[assignment]
    return manager, recorder


def test_observer_seq_matches_finalizer_transcript_seq() -> None:
    # (a) The Observer's view: the same turns as vera:transcript entries.
    manager, recorder = _manager_with_recorder()
    for role, source, text in _TURNS:
        manager._ingest(
            TranscriptEvent.model_validate({"role": role, "source": source, "text": text, "ts": 1})
        )

    # (b) The finalizer's view: the same turns as call-events envelopes, with a
    # call_status envelope interleaved (exists ONLY on this stream; must not
    # consume a seq slot).
    events = [
        CallStreamEvent(type=TYPE_TRANSCRIPT, data={"role": r, "source": s, "text": t}, ts=1)
        for r, s, t in _TURNS
    ]
    events.insert(2, CallStreamEvent(type=TYPE_CALL_STATUS, data={"status": "active"}, ts=1))
    ref = RoomRef(tenant_id=uuid.uuid4(), call_id=uuid.uuid4())
    rows, skipped = _build_rows(ref, events)
    assert skipped == 0

    # Both counters saw all five turns…
    assert len(recorder.turns) == len(_TURNS)
    assert len(rows) == len(_TURNS)
    # …and every turn (crucially each REP turn, whose seq becomes evidence_seq)
    # carries the identical seq in both numberings, keyed by (role, text).
    finalizer_seq = {(row["role"], row["message"]): row["seq"] for row in rows}
    for role, text, observer_seq in recorder.turns:
        assert finalizer_seq[(role, text)] == observer_seq, (role, text)
    rep_seqs = [seq for role, _, seq in recorder.turns if role == "user"]
    assert rep_seqs == [1, 4]  # dtmf occupied slot 2 in BOTH counters
