import pytest
from uuid import uuid4
from vera_core.integrations.llm import TranscriptTurn
from vera_core.transcript import InMemoryTranscriptStore, TranscriptService
from vera_core.observability.correlation import room_name_for_call
from control_plane.post_call import build_turns


@pytest.mark.asyncio
async def test_build_turns_enumerates_snapshot():
    store = InMemoryTranscriptStore()
    svc = TranscriptService(store)
    tenant_id, call_id = uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    await svc.publish_turn(room, "user", "hello", ts=1)
    await svc.publish_turn(room, "agent", "in network", ts=2)

    turns = await build_turns(svc, tenant_id, call_id)

    assert turns == [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]
