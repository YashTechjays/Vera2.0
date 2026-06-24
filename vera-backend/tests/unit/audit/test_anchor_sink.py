from pathlib import Path

import pytest

from vera_core.audit.anchor import (
    AnchorSink,
    LocalFilesystemAnchorSink,
    build_anchor_sink,
)
from vera_core.config.settings import Settings


async def test_local_sink_write_then_read_latest(tmp_path: Path) -> None:
    sink = LocalFilesystemAnchorSink(tmp_path)
    assert await sink.read_latest() is None
    await sink.write_anchor("anchors/2026/06/22/2026-06-22T00:00:00.000000-a.json", b"first")
    await sink.write_anchor("anchors/2026/06/22/2026-06-22T01:00:00.000000-b.json", b"second")
    assert await sink.read_latest() == b"second"  # lexicographically last key wins


async def test_local_sink_is_create_only(tmp_path: Path) -> None:
    sink = LocalFilesystemAnchorSink(tmp_path)
    await sink.write_anchor("anchors/x.json", b"one")
    with pytest.raises(FileExistsError):
        await sink.write_anchor("anchors/x.json", b"two")  # WORM: no overwrite


def test_build_anchor_sink_selects_local_when_no_bucket(tmp_path: Path) -> None:
    settings = Settings(audit_anchor_bucket=None, audit_anchor_local_dir=str(tmp_path))
    sink = build_anchor_sink(settings)
    assert isinstance(sink, LocalFilesystemAnchorSink)


def test_anchor_sink_protocol_is_runtime_checkable(tmp_path: Path) -> None:
    assert isinstance(LocalFilesystemAnchorSink(tmp_path), AnchorSink)
