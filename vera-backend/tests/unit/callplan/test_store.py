"""CallPlanStore: plan round-trip + the raw-value prefill builder. Redis is a
minimal in-memory stub (the store only uses set/get/delete)."""

from typing import Any

import pytest

from vera_core.callplan import (
    CallPlan,
    CallPlanStore,
    build_prefill,
    call_plan_key,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.data.pop(key, None)


def _store(redis: Any) -> CallPlanStore:
    return CallPlanStore(redis, ttl_seconds=60)


def _plan(room: str = "call--t--c") -> CallPlan:
    return CallPlan(
        room_name=room,
        tenant_id="t",
        call_id="c",
        schema_version_id="sv",
        greeting="hi",
        flat_instructions="do things",
    )


class TestPlanRoundTrip:
    @pytest.mark.asyncio
    async def test_put_get(self) -> None:
        store = _store(_FakeRedis())
        await store.put_plan(_plan())
        got = await store.get_plan("call--t--c")
        assert got is not None and got.flat_instructions == "do things"

    @pytest.mark.asyncio
    async def test_get_missing_is_none(self) -> None:
        assert await _store(_FakeRedis()).get_plan("nope") is None

    @pytest.mark.asyncio
    async def test_delete_clears_the_key(self) -> None:
        redis = _FakeRedis()
        store = _store(redis)
        await store.put_plan(_plan())
        await store.delete("call--t--c")
        assert redis.data == {}


class TestPrefill:
    def test_build_prefill_keeps_raw_values(self) -> None:
        prefill = build_prefill(
            {
                "patient_information.patient_name": "Jane Doe",
                "benefit_coverage.oon": True,  # bools → Yes/No for rule comparisons
                "x.empty": "  ",  # blank skipped
                "x.none": None,  # None skipped
            }
        )
        assert prefill == {
            "patient_information.patient_name": "Jane Doe",
            "benefit_coverage.oon": "Yes",
        }

    def test_key_helper(self) -> None:
        assert call_plan_key("r") == "vera:callplan:r"
