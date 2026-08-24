"""Call-content visibility: live calls are owner-or-published (spec
2026-07-22-recording-playback-ui); finished calls are tenant-visible (VR2-177)."""

from uuid import uuid4

from vera_core.services.call_visibility import (
    call_content_visible,
    call_hidden_from,
    recording_playable,
)

OWNER = uuid4()
OTHER = uuid4()


def test_owner_always_sees_their_call() -> None:
    assert call_hidden_from(OWNER, False, OWNER) is False
    assert call_hidden_from(OWNER, True, OWNER) is False


def test_non_owner_hidden_until_published() -> None:
    assert call_hidden_from(OWNER, False, OTHER) is True
    assert call_hidden_from(OWNER, True, OTHER) is False


def test_ownerless_call_is_tenant_visible() -> None:
    assert call_hidden_from(None, False, OTHER) is False
    assert call_hidden_from(None, False, None) is False


def test_finished_call_content_is_tenant_visible() -> None:
    # Every terminal status opens the content; a live one keeps the owner rule.
    for status in ("completed", "failed", "no_answer", "busy", "canceled"):
        assert call_content_visible(OWNER, False, OTHER, status=status) is True
    assert call_content_visible(OWNER, False, OTHER, status="active") is False
    assert call_content_visible(OWNER, False, OWNER, status="active") is True
    assert call_content_visible(OWNER, True, OTHER, status="ringing") is True


def _playable(*, has_recording: bool = True, can_play: bool = True) -> bool:
    """recording_playable on a finished call OWNER owns, unpublished, viewed by OTHER."""
    return recording_playable(
        has_recording=has_recording,
        initiated_by_id=OWNER,
        published=False,
        user_id=OTHER,
        can_play=can_play,
        status="completed",
    )


def test_recording_playable_requires_recordings_read_even_when_terminal() -> None:
    assert _playable() is True
    assert _playable(can_play=False) is False
    assert _playable(has_recording=False) is False
