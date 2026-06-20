from agent_worker.main import resolve_session, session_id_for

_VERA_ROOM = "call--11111111-1111-7111-8111-111111111111--22222222-2222-7222-8222-222222222222"


def test_session_id_is_room_name() -> None:
    assert session_id_for(_VERA_ROOM) == _VERA_ROOM


def test_resolve_session_real_call_room_runs_in_prod() -> None:
    # A canonical vera call room always resolves to its room name as the session id,
    # regardless of environment.
    assert resolve_session(_VERA_ROOM, is_local=False) == _VERA_ROOM
    assert resolve_session(_VERA_ROOM, is_local=True) == _VERA_ROOM


def test_resolve_session_foreign_room_rejected_outside_local() -> None:
    # A non-vera room (e.g. console/connect mic test) must be rejected in any
    # non-local environment — the agent never attaches to a foreign room in prod.
    assert resolve_session("some-console-room", is_local=False) is None


def test_resolve_session_foreign_room_runs_in_local() -> None:
    # Local dev: a foreign room is the livekit `console`/`connect` mic test; it runs
    # with a synthetic session id so the cascade can be exercised without a call.
    assert resolve_session("some-console-room", is_local=True) == "some-console-room"


def test_resolve_session_empty_room_falls_back_to_console_in_local() -> None:
    assert resolve_session("", is_local=True) == "console"
