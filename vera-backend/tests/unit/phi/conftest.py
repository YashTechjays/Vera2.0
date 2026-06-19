from uuid import UUID

import pytest

from phi_codec.codec import PHICodec
from phi_codec.config import CodecConfig
from vera_core.audit import AuditRecord
from vera_core.db import uuid7
from vera_core.phi import PHIBoundary


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def emit(self, record: AuditRecord) -> None:
        self.records.append(record)

    def events(self, event_type: str) -> list[AuditRecord]:
        return [r for r in self.records if r.event_type == event_type]


@pytest.fixture(scope="session")
def codec() -> PHICodec:
    # GLiNER off: deterministic regex+spaCy path, same default as the voice runtime.
    return PHICodec(CodecConfig(use_gliner=False))


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def tenant_id() -> UUID:
    return uuid7()


@pytest.fixture
def boundary(codec: PHICodec, sink: RecordingSink, tenant_id: UUID) -> PHIBoundary:
    return PHIBoundary(codec, sink, tenant_id)
