import pytest

from vera_core.phi import PassthroughPHIBoundary, PHIBoundaryProtocol


@pytest.mark.asyncio
async def test_passthrough_is_identity_and_satisfies_protocol() -> None:
    b = PassthroughPHIBoundary()
    assert isinstance(b, PHIBoundaryProtocol)

    await b.open_session("s1", {"name": "Jane"})  # accepted, no-op
    assert await b.redact("s1", "Jane Doe, member 123") == "Jane Doe, member 123"
    assert await b.hydrate_for_speech("s1", "[[NAME_1]]") == "[[NAME_1]]"
    assert await b.hydrate_raw("s1", {"member": "[[ID_1]]"}) == {"member": "[[ID_1]]"}
    await b.close_session("s1")  # no-op, must not raise
