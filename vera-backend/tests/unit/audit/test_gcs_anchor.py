import sys
import types
from unittest.mock import MagicMock

import pytest

from vera_core.audit.anchor import AnchorSink


@pytest.fixture
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Inject a fake google.cloud.storage module so the lazy import resolves."""
    storage = MagicMock(name="storage")
    google = types.ModuleType("google")
    google_cloud = types.ModuleType("google.cloud")
    google_cloud.storage = storage  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage)
    return storage


async def test_write_anchor_is_create_only(fake_storage: MagicMock) -> None:
    from vera_core.audit.gcs_anchor import GCSAnchorSink

    blob = fake_storage.Client.return_value.bucket.return_value.blob.return_value
    sink = GCSAnchorSink("my-bucket", "audit-anchors")
    await sink.write_anchor("anchors/2026/06/22/x.json", b"body")

    fake_storage.Client.return_value.bucket.assert_called_once_with("my-bucket")
    fake_storage.Client.return_value.bucket.return_value.blob.assert_called_once_with(
        "audit-anchors/anchors/2026/06/22/x.json"
    )
    blob.upload_from_string.assert_called_once()
    assert blob.upload_from_string.call_args.kwargs["if_generation_match"] == 0


def test_gcs_sink_satisfies_protocol(fake_storage: MagicMock) -> None:
    from vera_core.audit.gcs_anchor import GCSAnchorSink

    assert isinstance(GCSAnchorSink("b", "p"), AnchorSink)


async def test_read_latest_returns_greatest_named_blob(fake_storage: MagicMock) -> None:
    from vera_core.audit.gcs_anchor import GCSAnchorSink

    older = MagicMock(name="2026-06-21")
    older.name = "audit-anchors/anchors/2026/06/21/a.json"
    newer = MagicMock(name="2026-06-22")
    newer.name = "audit-anchors/anchors/2026/06/22/b.json"
    newer.download_as_bytes.return_value = b"newest-body"
    # Returned out of order to prove selection is by name, not list order.
    fake_storage.Client.return_value.list_blobs.return_value = [newer, older]

    sink = GCSAnchorSink("my-bucket", "audit-anchors")
    result = await sink.read_latest()

    fake_storage.Client.return_value.list_blobs.assert_called_once_with(
        "my-bucket", prefix="audit-anchors/anchors/"
    )
    newer.download_as_bytes.assert_called_once()
    older.download_as_bytes.assert_not_called()
    assert result == b"newest-body"
    assert isinstance(result, bytes)


async def test_read_latest_returns_none_when_empty(fake_storage: MagicMock) -> None:
    from vera_core.audit.gcs_anchor import GCSAnchorSink

    fake_storage.Client.return_value.list_blobs.return_value = []
    sink = GCSAnchorSink("my-bucket", "audit-anchors")
    assert await sink.read_latest() is None
