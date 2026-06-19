"""The three boundary crossings: redact (inbound), hydrate_for_speech
(fail-safe outbound), hydrate_raw (strict outbound). Pure unit tests — no DB,
audit captured by a recording sink."""

import pytest

from phi_codec.tokens.token import TOKEN_RE
from vera_core.models.audit_log import AuditEvent
from vera_core.phi import NEUTRAL_PHRASE, PHIBoundary, UnresolvedPHITokenError

from .conftest import RecordingSink

MEMBER_NAME = "John Smith"
MEMBER_ID = "XYZ987654321"


async def _seeded(boundary: PHIBoundary, session_id: str) -> None:
    await boundary.open_session(
        session_id, known={"NAME": MEMBER_NAME, "BENEFICIARY_ID": MEMBER_ID}
    )


async def test_redact_masks_seeded_phi(boundary: PHIBoundary, sink: RecordingSink) -> None:
    await _seeded(boundary, "s1")
    redacted = await boundary.redact("s1", f"Calling about {MEMBER_NAME}, member ID {MEMBER_ID}.")
    assert MEMBER_NAME not in redacted
    assert MEMBER_ID not in redacted
    assert TOKEN_RE.search(redacted), "expected [[TYPE_N]] tokens in redacted text"
    assert sink.events(AuditEvent.PHI_ACCESS.value), "redact must be audited"
    await boundary.close_session("s1")


async def test_redact_detects_unseeded_ssn(boundary: PHIBoundary) -> None:
    await boundary.open_session("s2")
    redacted = await boundary.redact("s2", "The SSN is 856-45-6789.")
    assert "856-45-6789" not in redacted
    await boundary.close_session("s2")


async def test_hydrate_for_speech_round_trip(boundary: PHIBoundary) -> None:
    await _seeded(boundary, "s3")
    redacted = await boundary.redact("s3", f"Patient {MEMBER_NAME}, member {MEMBER_ID}.")
    spoken = await boundary.hydrate_for_speech("s3", redacted)
    assert MEMBER_NAME in spoken  # names read back as natural language
    assert "X Y Z 9 8 7 6 5 4 3 2 1" in spoken  # IDs are spelled for TTS
    assert not TOKEN_RE.search(spoken)
    await boundary.close_session("s3")


async def test_hydrate_for_speech_failsafe_on_unknown_token(
    boundary: PHIBoundary, sink: RecordingSink
) -> None:
    await boundary.open_session("s4")
    spoken = await boundary.hydrate_for_speech("s4", "Your ID is [[BENEFICIARY_ID_7]], correct?")
    assert "[[BENEFICIARY_ID_7]]" not in spoken
    assert NEUTRAL_PHRASE in spoken
    failsafes = sink.events(AuditEvent.PHI_HYDRATE_FAILSAFE.value)
    assert len(failsafes) == 1
    assert failsafes[0].detail["unresolved"] == ["[[BENEFICIARY_ID_7]]"]
    await boundary.close_session("s4")


async def test_hydrate_raw_resolves_nested_args(boundary: PHIBoundary, sink: RecordingSink) -> None:
    await _seeded(boundary, "s5")
    redacted = await boundary.redact("s5", f"Member {MEMBER_ID}, name {MEMBER_NAME}.")
    tokens = [m.group(0) for m in TOKEN_RE.finditer(redacted)]
    assert len(tokens) >= 2
    args = {"member": {"id": tokens[0]}, "names": [tokens[1]], "note": "no tokens here"}
    resolved = await boundary.hydrate_raw("s5", args)
    flat = str(resolved)
    assert MEMBER_ID in flat and MEMBER_NAME in flat
    assert not TOKEN_RE.search(flat)
    allows = [r for r in sink.events(AuditEvent.PHI_DETOKENIZE.value) if r.decision == "allow"]
    assert len(allows) == 1
    await boundary.close_session("s5")


async def test_hydrate_raw_strict_raises_and_audits(
    boundary: PHIBoundary, sink: RecordingSink
) -> None:
    await boundary.open_session("s6")
    with pytest.raises(UnresolvedPHITokenError) as exc:
        await boundary.hydrate_raw("s6", {"member_id": "[[BENEFICIARY_ID_9]]"})
    assert exc.value.tokens == ["[[BENEFICIARY_ID_9]]"]
    denies = [r for r in sink.events(AuditEvent.PHI_DETOKENIZE.value) if r.decision == "deny"]
    assert len(denies) == 1
    await boundary.close_session("s6")


async def test_close_session_wipes_vault(boundary: PHIBoundary) -> None:
    await _seeded(boundary, "s7")
    redacted = await boundary.redact("s7", f"Name {MEMBER_NAME}.")
    token = TOKEN_RE.search(redacted)
    assert token is not None
    await boundary.close_session("s7")

    await boundary.open_session("s7")  # same id, new call: vault must be empty
    spoken = await boundary.hydrate_for_speech("s7", f"Hello {token.group(0)}")
    assert MEMBER_NAME not in spoken
    assert NEUTRAL_PHRASE in spoken
    await boundary.close_session("s7")


async def test_audit_records_never_contain_raw_phi(
    boundary: PHIBoundary, sink: RecordingSink
) -> None:
    await _seeded(boundary, "s8")
    redacted = await boundary.redact("s8", f"{MEMBER_NAME} member {MEMBER_ID}")
    await boundary.hydrate_for_speech("s8", redacted)
    dump = str([r.detail for r in sink.records]) + str([r.reason for r in sink.records])
    assert MEMBER_NAME not in dump
    assert MEMBER_ID not in dump
    await boundary.close_session("s8")
