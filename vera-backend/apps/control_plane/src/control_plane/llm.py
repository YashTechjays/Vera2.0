"""Vertex AI Gemini implementation of the post-call LLMClient. Structured output;
Flash by default. Consumes only the de-identified transcript — no raw PHI."""

import json
from typing import Any

from google import genai
from google.genai import types

from vera_core.integrations.llm import ExtractedField, JudgeVerdict, LLMClient, TranscriptTurn


def _turns_block(turns: list[TranscriptTurn]) -> str:
    return "\n".join(f"[{t.seq}] {t.role}: {t.text}" for t in turns)


def build_extract_prompt(field_paths: list[str], turns: list[TranscriptTurn]) -> str:
    return (
        "You are extracting insurance-benefit answers from a de-identified call "
        "transcript. Turns are numbered [n]. For each requested field_path, return the "
        "value stated by the payer, a 0-100 confidence, and evidence_seq = the [n] of the "
        "turn that supports it. Omit fields not present. Do NOT invent values.\n\n"
        f"field_paths:\n{json.dumps(field_paths)}\n\ntranscript:\n{_turns_block(turns)}"
    )


def parse_extract_response(data: list[dict[str, Any]]) -> list[ExtractedField]:
    return [
        ExtractedField(
            field_path=str(d["field_path"]),
            value=str(d["value"]),
            confidence=int(d["confidence"]),
            evidence_seq=int(d["evidence_seq"]),
        )
        for d in data
    ]


def build_judge_prompt(extracted: list[ExtractedField], turns: list[TranscriptTurn]) -> str:
    items = [
        {"field_path": e.field_path, "value": e.value, "evidence_seq": e.evidence_seq}
        for e in extracted
    ]
    return (
        "For each extracted field, decide whether the transcript SUPPORTS the value. "
        "Return supported (bool), 0-100 confidence, and a short evidence quote.\n\n"
        f"extracted:\n{json.dumps(items)}\n\ntranscript:\n{_turns_block(turns)}"
    )


def parse_judge_response(data: list[dict[str, Any]]) -> list[JudgeVerdict]:
    return [
        JudgeVerdict(
            field_path=str(d["field_path"]),
            supported=bool(d["supported"]),
            confidence=int(d["confidence"]),
            evidence=str(d["evidence"]),
        )
        for d in data
    ]


_EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "field_path": {"type": "string"},
            "value": {"type": "string"},
            "confidence": {"type": "integer"},
            "evidence_seq": {"type": "integer"},
        },
        "required": ["field_path", "value", "confidence", "evidence_seq"],
    },
}
_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "field_path": {"type": "string"},
            "supported": {"type": "boolean"},
            "confidence": {"type": "integer"},
            "evidence": {"type": "string"},
        },
        "required": ["field_path", "supported", "confidence", "evidence"],
    },
}


class VertexLLMClient(LLMClient):
    def __init__(self, *, project: str, location: str, model: str) -> None:
        self._client = genai.Client(vertexai=True, project=project, location=location)
        self._model = model

    async def _generate(self, prompt: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
        resp = await self._client.aio.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        return json.loads(resp.text)  # type: ignore[arg-type,no-any-return]

    async def extract(
        self, *, field_paths: list[str], turns: list[TranscriptTurn]
    ) -> list[ExtractedField]:
        data = await self._generate(build_extract_prompt(field_paths, turns), _EXTRACT_SCHEMA)
        return parse_extract_response(data)

    async def judge(
        self, *, extracted: list[ExtractedField], turns: list[TranscriptTurn]
    ) -> list[JudgeVerdict]:
        if not extracted:
            return []
        data = await self._generate(build_judge_prompt(extracted, turns), _JUDGE_SCHEMA)
        return parse_judge_response(data)
