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
