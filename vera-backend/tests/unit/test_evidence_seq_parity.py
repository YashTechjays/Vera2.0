"""Pin evidence_seq ↔ transcript.seq parity (the Observer↔finalizer contract).

Both now read the SAME stream (`vera:call-events:{room}`), so parity is no longer about two
transports staying in sync — it is about two *counters* applying identical skip rules to one
envelope sequence. `ObserverManager.ingest` stamps `evidence_seq`; `_build_rows` numbers the
persisted `transcript.seq`. Each must skip — WITHOUT consuming a slot — both
(a) non-transcript envelopes (`call_status`) and (b) turns whose source won't resolve.
This drives one mixed sequence through both and asserts every turn lands on the same seq, so a
drift in either counter fails here instead of silently mispointing `field_answer.evidence_seq`.
"""

import uuid
from typing import Any

from agent_worker.observer import ObserverManager
from control_plane.transcript_finalizer import _build_rows
from vera_core.call_stream import TYPE_CALL_STATUS, TYPE_TRANSCRIPT, CallStreamEvent
from vera_core.forms.call_plan import CallPlan, PlanSession, PlanTask
from vera_core.models.enums import TranscriptSource
from vera_core.observability.correlation import RoomRef
from vera_core.transcript import _VALID_SOURCES

ROOM = "call--t--c"

# One canonical call as it appears on the single stream. The dtmf turn matters (it DOES
# occupy a slot in both counters); the call_status frame and the bad-source turn must not.
_EVENTS: list[CallStreamEvent] = [
    CallStreamEvent(type=TYPE_CALL_STATUS, data={"status": "active"}, ts=1),
    CallStreamEvent(
        type=TYPE_TRANSCRIPT,
        data={"role": "agent", "source": "bot", "text": "What is the member ID?"},
        ts=1,
    ),
    CallStreamEvent(
        type=TYPE_TRANSCRIPT, data={"role": "user", "source": "rep", "text": "ABC123."}, ts=1
    ),
    CallStreamEvent(
        type=TYPE_TRANSCRIPT, data={"role": "dtmf", "source": "bot", "text": "1"}, ts=1
    ),
    # Corrupt envelope: unknown source AND unknown role → unresolvable, skipped by both.
    CallStreamEvent(
        type=TYPE_TRANSCRIPT, data={"role": "???", "source": "???", "text": "junk"}, ts=1
    ),
    CallStreamEvent(
        type=TYPE_TRANSCRIPT,
        data={"role": "user", "source": "supervisor", "text": "Is the deductible met?"},
        ts=1,
    ),
    CallStreamEvent(
        type=TYPE_TRANSCRIPT,
        data={"role": "user", "source": "rep", "text": "Yes, fully met."},
        ts=1,
    ),
    CallStreamEvent(type=TYPE_CALL_STATUS, data={"status": "ended"}, ts=1),
]


class _RecordingObserver:
    """Stands in for the active TaskObserver; records the seq the manager stamps."""

    def __init__(self) -> None:
        self.turns: list[tuple[str, str, int]] = []  # (role, text, seq)

    def feed(self, turn: Any) -> None:
        self.turns.append((turn.role, turn.text, turn.seq))


class _Null:
    """Inert stand-in for the manager's collaborators (never touched by ingest)."""

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
    # Pin the active observer so ingest routes every turn to the recorder without
    # rotating (active_task_index stays 0 == _active_index).
    manager._active_index = 0
    manager._active = recorder  # type: ignore[assignment]
    return manager, recorder


def test_observer_seq_matches_finalizer_transcript_seq() -> None:
    manager, recorder = _manager_with_recorder()
    for event in _EVENTS:
        manager.ingest(event)

    ref = RoomRef(tenant_id=uuid.uuid4(), call_id=uuid.uuid4())
    rows, skipped = _build_rows(ref, _EVENTS)

    assert skipped == 1  # the corrupt envelope, dropped by the finalizer
    # Both counters kept exactly the same five turns (6 transcript envelopes - 1 corrupt)…
    assert len(recorder.turns) == len(rows) == 5
    # …and each turn carries the identical seq in both numberings, keyed by (role, text).
    finalizer_seq = {(row["role"], row["message"]): row["seq"] for row in rows}
    for role, text, observer_seq in recorder.turns:
        assert finalizer_seq[(role, text)] == observer_seq, (role, text)
    # Concretely: the two status frames and the corrupt turn consumed NO slot; dtmf did.
    assert [seq for _, _, seq in recorder.turns] == [0, 1, 2, 3, 4]
    # The evidence-bearing rep answer is seq 4 — what evidence_seq would point at.
    assert [seq for _, text, seq in recorder.turns if text == "Yes, fully met."] == [4]


def test_accepted_sources_match_the_persisted_source_enum() -> None:
    # `resolve_turn_source` validates a stamped source against vera_core.transcript's own
    # literals (keeping that module ORM-free); they must stay equal to the CHECK-constrained
    # column enum, or a source valid on the wire would be rejected (or persisted) wrongly.
    assert {source.value for source in TranscriptSource} == _VALID_SOURCES


def test_call_status_frame_never_rotates_the_task() -> None:
    # The type filter must run BEFORE the rotate check, or a status frame would swap the
    # active TaskObserver (dropping the outgoing one's window mid-task).
    manager, recorder = _manager_with_recorder()
    manager._active_index = 0

    class _Moved:
        active_task_index = 1  # a rotation would fire if the frame got past the filter

    manager._controller = _Moved()  # type: ignore[assignment]
    manager.ingest(CallStreamEvent(type=TYPE_CALL_STATUS, data={"status": "active"}, ts=1))
    active: object = manager._active  # widened: the recorder is a structural stand-in
    assert active is recorder  # not rotated
    assert manager._seq == 0  # and no slot consumed
