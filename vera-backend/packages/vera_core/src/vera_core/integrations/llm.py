"""LLM seam for the post-call re-read — a pure Protocol + DTOs, no provider SDK.

The concrete Vertex/Gemini client lives in the control plane (control_plane.llm);
vera_core only knows this interface so the eval service stays provider-agnostic and
unit-testable with FakeLLMClient.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TranscriptTurn:
    """One de-identified transcript turn handed to the LLM. `seq` is the 0-based
    index within the call's snapshot — the stable pointer stored as evidence_seq."""

    seq: int
    role: str
    text: str


@dataclass(frozen=True)
class ExtractedField:
    field_path: str
    value: str
    confidence: int  # 0-100
    evidence_seq: int  # index into the turns list


@dataclass(frozen=True)
class JudgeVerdict:
    field_path: str
    supported: bool
    confidence: int  # 0-100
    evidence: str


class LLMClient(Protocol):
    async def extract(
        self, *, field_paths: list[str], turns: list[TranscriptTurn]
    ) -> list[ExtractedField]: ...

    async def judge(
        self, *, extracted: list[ExtractedField], turns: list[TranscriptTurn]
    ) -> list[JudgeVerdict]: ...


class FakeLLMClient:
    """Deterministic test double. Records each call's arguments so tests can
    assert WHAT was extracted/judged (e.g. top-up extraction only receives the
    missing paths)."""

    def __init__(
        self,
        *,
        extracted: list[ExtractedField],
        verdicts: list[JudgeVerdict],
        raise_on_extract: Exception | None = None,
    ) -> None:
        self._extracted = extracted
        self._verdicts = verdicts
        self._raise_on_extract = raise_on_extract
        self.extract_calls: list[list[str]] = []
        self.judge_calls: list[list[ExtractedField]] = []

    async def extract(
        self, *, field_paths: list[str], turns: list[TranscriptTurn]
    ) -> list[ExtractedField]:
        self.extract_calls.append(list(field_paths))
        if self._raise_on_extract is not None:
            raise self._raise_on_extract
        return list(self._extracted)

    async def judge(
        self, *, extracted: list[ExtractedField], turns: list[TranscriptTurn]
    ) -> list[JudgeVerdict]:
        self.judge_calls.append(list(extracted))
        return list(self._verdicts)
