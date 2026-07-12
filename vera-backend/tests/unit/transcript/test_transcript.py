import asyncio

import pytest

from vera_core.transcript import (
    ROLE_AGENT,
    ROLE_DTMF,
    ROLE_USER,
    SOURCE_BOT,
    SOURCE_REP,
    InMemoryTranscriptStore,
    TranscriptEvent,
    TranscriptService,
    source_for_role,
    transcript_stream_key,
)


def test_stream_key_pattern() -> None:
    assert transcript_stream_key("call--t--c") == "vera:transcript:call--t--c"


def test_source_for_role_maps_every_live_role() -> None:
    # "user" is the human on the line (payer rep), everything the agent does — speech
    # or a keypad press — is the bot.
    assert source_for_role(ROLE_USER) == SOURCE_REP
    assert source_for_role(ROLE_AGENT) == SOURCE_BOT
    assert source_for_role(ROLE_DTMF) == SOURCE_BOT


def test_event_derives_source_from_role_when_absent() -> None:
    # Legacy stream entries (pre-source) parse into events with the derived source, so
    # consumers never see a source-less turn mid-deploy.
    legacy_user = TranscriptEvent.model_validate({"role": ROLE_USER, "text": "hi", "ts": 1})
    legacy_agent = TranscriptEvent.model_validate({"role": ROLE_AGENT, "text": "hi", "ts": 1})
    assert legacy_user.source == SOURCE_REP
    assert legacy_agent.source == SOURCE_BOT


def test_event_keeps_an_explicit_source() -> None:
    event = TranscriptEvent(role=ROLE_DTMF, source=SOURCE_BOT, text="3", ts=1)
    assert event.source == SOURCE_BOT


def _service() -> TranscriptService:
    return TranscriptService(InMemoryTranscriptStore())


@pytest.mark.asyncio
async def test_publish_then_collect_in_order() -> None:
    svc = _service()
    await svc.publish_turn("room", ROLE_USER, "hi", ts=1)
    await svc.publish_turn("room", ROLE_AGENT, "hello", ts=2)
    await svc.end("room")
    got = await svc.collect("room")
    assert [(e.role, e.text) for e in got] == [(ROLE_USER, "hi"), (ROLE_AGENT, "hello")]


@pytest.mark.asyncio
async def test_publish_turn_source_round_trips_through_the_store() -> None:
    svc = _service()
    await svc.publish_turn("room", ROLE_USER, "hi", ts=1)  # source derived from role
    await svc.publish_turn("room", ROLE_DTMF, "3", ts=2, source=SOURCE_BOT)  # explicit
    await svc.end("room")
    got = await svc.collect("room")
    assert [(e.role, e.source, e.text) for e in got] == [
        (ROLE_USER, SOURCE_REP, "hi"),
        (ROLE_DTMF, SOURCE_BOT, "3"),
    ]


@pytest.mark.asyncio
async def test_consume_tails_live_then_ends() -> None:
    svc = _service()
    seen: list[str] = []

    async def consume() -> None:
        async for _id, event in svc.consume("room"):
            seen.append(event.text)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # reader starts and blocks (stream empty)
    await svc.publish_turn("room", ROLE_USER, "live", ts=1)
    await svc.end("room")
    await asyncio.wait_for(task, timeout=1.0)
    assert seen == ["live"]


@pytest.mark.asyncio
async def test_consume_yields_unique_entry_ids() -> None:
    svc = _service()
    await svc.publish_turn("room", ROLE_USER, "a", ts=1)
    await svc.publish_turn("room", ROLE_USER, "b", ts=2)
    await svc.end("room")
    ids = [entry_id async for entry_id, _e in svc.consume("room")]
    assert len(ids) == len(set(ids)) == 2
