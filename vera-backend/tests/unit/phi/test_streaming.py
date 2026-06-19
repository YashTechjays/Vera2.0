"""Tokens straddling stream chunks must hydrate atomically before TTS."""

from phi_codec.tokens.token import TOKEN_RE
from vera_core.phi import NEUTRAL_PHRASE, PHIBoundary, SpeechStreamHydrator

MEMBER_NAME = "John Smith"


async def _hydrate_stream(boundary: PHIBoundary, session_id: str, chunks: list[str]) -> str:
    hydrator = SpeechStreamHydrator(boundary, session_id)
    out = ""
    for chunk in chunks:
        out += await hydrator.feed(chunk)
    out += await hydrator.flush()
    return out


async def _token_for_name(boundary: PHIBoundary, session_id: str) -> str:
    await boundary.open_session(session_id, known={"NAME": MEMBER_NAME})
    redacted = await boundary.redact(session_id, f"Patient {MEMBER_NAME}.")
    match = TOKEN_RE.search(redacted)
    assert match is not None
    token: str = match.group(0)
    return token


async def test_token_split_across_two_chunks(boundary: PHIBoundary) -> None:
    token = await _token_for_name(boundary, "st1")  # e.g. [[NAME_1]]
    out = await _hydrate_stream(boundary, "st1", [f"Hello {token[:4]}", f"{token[4:]} bye"])
    assert MEMBER_NAME in out
    assert not TOKEN_RE.search(out)
    await boundary.close_session("st1")


async def test_token_split_one_char_at_a_time(boundary: PHIBoundary) -> None:
    token = await _token_for_name(boundary, "st2")
    out = await _hydrate_stream(boundary, "st2", [f"Hi {token[0]}", *token[1:], " done"])
    assert MEMBER_NAME in out
    assert not TOKEN_RE.search(out)
    await boundary.close_session("st2")


async def test_plain_text_streams_through(boundary: PHIBoundary) -> None:
    await boundary.open_session("st3")
    out = await _hydrate_stream(boundary, "st3", ["Hello ", "there, ", "how are you?"])
    assert out == "Hello there, how are you?"
    await boundary.close_session("st3")


async def test_ordinary_brackets_do_not_stall_the_stream(boundary: PHIBoundary) -> None:
    await boundary.open_session("st4")
    hydrator = SpeechStreamHydrator(boundary, "st4")
    out = await hydrator.feed("see [note three] for details, and more text after that. ")
    # the bracket closed without becoming a token — nothing should be held forever
    out += await hydrator.feed("the end.")
    out += await hydrator.flush()
    assert "[note three]" in out
    assert out.endswith("the end.")
    await boundary.close_session("st4")


async def test_unknown_token_in_stream_is_neutralized(boundary: PHIBoundary) -> None:
    await boundary.open_session("st5")
    out = await _hydrate_stream(boundary, "st5", ["Your SSN is [[SS", "N_3]] right?"])
    assert "[[SSN_3]]" not in out
    assert NEUTRAL_PHRASE in out
    await boundary.close_session("st5")
