from livekit.agents import NOT_GIVEN

from agent_worker.main import build_room_input_options


def test_pins_speaker_and_ends_call_on_hangup() -> None:
    # Voice Lab pins the resolved speaker (browser caller / SIP callee) so RoomIO does not
    # link the listen-only monitor; close_on_disconnect + delete_room_on_close end the whole
    # call (drop the monitor, hang up the SIP leg) when that speaker hangs up.
    opts = build_room_input_options("phone-callee")
    assert opts.participant_identity == "phone-callee"
    assert opts.close_on_disconnect is True
    assert opts.delete_room_on_close is True


def test_auto_links_when_no_speaker_pinned() -> None:
    # The /calls path passes no speaker: RoomIO auto-links the sole participant, but the same
    # teardown policy still applies so a callee hangup ends the call.
    opts = build_room_input_options(NOT_GIVEN)
    assert opts.participant_identity is NOT_GIVEN
    assert opts.close_on_disconnect is True
    assert opts.delete_room_on_close is True
