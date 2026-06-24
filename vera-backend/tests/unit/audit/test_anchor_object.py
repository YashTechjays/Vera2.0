import hashlib
import json
from uuid import UUID

from vera_core.audit.anchor import (
    GENESIS_ANCHOR,
    ChainHead,
    anchor_key,
    build_anchor_object,
)


def _head(seq: int) -> ChainHead:
    return ChainHead(
        tenant_id=UUID("11111111-1111-1111-1111-111111111111"),
        head_seq=seq,
        head_row_hash=b"\xab" * 32,
        row_count=seq,
    )


def test_build_anchor_object_is_deterministic_and_self_hashing() -> None:
    run_id = UUID("22222222-2222-2222-2222-222222222222")
    obj, body = build_anchor_object(
        [_head(3)], GENESIS_ANCHOR, run_id, "2026-06-22T00:00:00.000000"
    )
    assert obj["run_id"] == str(run_id)
    assert obj["prev_anchor_sha256"] == GENESIS_ANCHOR.hex()
    assert obj["chains"][0]["head_row_hash"] == ("ab" * 32)
    # anchor_sha256 = sha256 over the canonical core (everything except anchor_sha256)
    core = {k: v for k, v in obj.items() if k != "anchor_sha256"}
    expected = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert obj["anchor_sha256"] == expected
    assert json.loads(body) == obj


def test_anchor_key_partitions_by_date() -> None:
    run_id = UUID("22222222-2222-2222-2222-222222222222")
    key = anchor_key("2026-06-22T01:02:03.000000", run_id)
    assert key == f"anchors/2026/06/22/2026-06-22T01:02:03.000000-{run_id}.json"
