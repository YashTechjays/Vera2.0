"""RecordingStorage contract via the in-memory fake + gs:// parsing."""

import hashlib

import pytest

from control_plane.recording_storage import InMemoryRecordingStorage, parse_gcs_uri


def test_parse_gcs_uri() -> None:
    assert parse_gcs_uri("gs://bkt/a/b/c.ogg") == ("bkt", "a/b/c.ogg")
    with pytest.raises(ValueError):
        parse_gcs_uri("https://bkt/a.ogg")


async def test_sha256_and_size_roundtrip() -> None:
    store = InMemoryRecordingStorage()
    body = b"fake-ogg-bytes"
    store.objects[("bkt", "t/c.ogg")] = body
    result = await store.sha256_and_size("bkt", "t/c.ogg")
    assert result == (hashlib.sha256(body).hexdigest(), len(body))
    assert await store.sha256_and_size("bkt", "missing.ogg") is None


async def test_delete_is_idempotent_and_exists_flips() -> None:
    store = InMemoryRecordingStorage()
    store.objects[("bkt", "x.ogg")] = b"x"
    assert await store.exists("bkt", "x.ogg")
    await store.delete("bkt", "x.ogg")
    await store.delete("bkt", "x.ogg")  # absent → no-op, no raise
    assert not await store.exists("bkt", "x.ogg")


async def test_signed_url_embeds_ttl() -> None:
    store = InMemoryRecordingStorage()
    store.objects[("bkt", "x.ogg")] = b"x"
    url = await store.signed_url("bkt", "x.ogg", ttl_seconds=600)
    assert url.startswith("https://") and "x.ogg" in url and "600" in url
