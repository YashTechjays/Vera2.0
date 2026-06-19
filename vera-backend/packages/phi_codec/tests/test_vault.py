"""Vault: dedup, encryption-at-rest, session isolation, lifecycle."""

import pytest

from phi_codec.vault.crypto import FernetEncryptor
from phi_codec.vault.memory_vault import InMemoryVault

pytestmark = pytest.mark.asyncio


async def test_same_value_same_token_counter_advances_only_for_new():
    v = InMemoryVault()
    await v.open_session("s")
    t1 = await v.get_or_create_token("s", "NAME", "John Smith", turn_id="t1", recognizer="x", score=1.0)
    t2 = await v.get_or_create_token("s", "NAME", "John Smith", turn_id="t2", recognizer="x", score=1.0)
    t3 = await v.get_or_create_token("s", "NAME", "Jane Doe", turn_id="t2", recognizer="x", score=1.0)
    assert t1 == t2 == "[[NAME_1]]"
    assert t3 == "[[NAME_2]]"


async def test_raw_values_are_encrypted_at_rest():
    enc = FernetEncryptor()
    v = InMemoryVault(encryptor=enc)
    await v.open_session("s")
    await v.get_or_create_token("s", "SSN", "521238765", turn_id="t1", recognizer="x", score=1.0)
    # The stored ciphertext must not contain the plaintext.
    stored = v._sessions["s"].reverse["[[SSN_1]]"].ciphertext
    assert b"521238765" not in stored
    assert enc.decrypt(stored) == "521238765"


async def test_resolve_round_trips():
    v = InMemoryVault()
    await v.open_session("s")
    tok = await v.get_or_create_token("s", "NAME", "John Smith", turn_id="t1", recognizer="x", score=0.9)
    entry = await v.resolve("s", tok)
    assert entry is not None and entry.raw_value == "John Smith" and entry.entity_type == "NAME"
    assert await v.resolve("s", "[[NAME_99]]") is None


async def test_sessions_are_isolated_and_destroyed_on_close():
    v = InMemoryVault()
    await v.open_session("a")
    await v.open_session("b")
    await v.get_or_create_token("a", "NAME", "John Smith", turn_id="t1", recognizer="x", score=1.0)
    assert await v.resolve("b", "[[NAME_1]]") is None  # isolation
    await v.close_session("a")
    with pytest.raises(KeyError):
        await v.resolve("a", "[[NAME_1]]")  # destroyed
