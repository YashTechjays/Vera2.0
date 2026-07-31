"""Vertex AI Gemini implementation of the post-call LLMClient. Structured output;
Flash by default. Consumes only the de-identified transcript — no raw PHI."""

import json
from typing import Any

from google import genai
from google.genai import types

from vera_core.forms.review import is_blank_answer
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


def _clamp_confidence(raw: Any) -> int:
    """Bound an LLM-reported confidence to [0, 100] — the DB check constraint
    (`confidence_range`) rejects anything outside it, and structured output
    doesn't guarantee numeric bounds. An IntegrityError here would leave the
    job unacked and re-bill the eval on every reclaim."""
    return max(0, min(100, int(raw)))


def parse_extract_response(data: list[dict[str, Any]]) -> list[ExtractedField]:
    # the response schema forces a value on every item, so unheard fields arrive as "" (VR2-93)
    return [
        ExtractedField(
            field_path=str(d["field_path"]),
            value=str(d["value"]),
            confidence=_clamp_confidence(d["confidence"]),
            evidence_seq=int(d["evidence_seq"]),
        )
        for d in data
        if not is_blank_answer(d["value"])
    ]


def build_judge_prompt(extracted: list[ExtractedField], turns: list[TranscriptTurn]) -> str:
    items = [
        {"field_path": e.field_path, "value": e.value, "evidence_seq": e.evidence_seq}
        for e in extracted
    ]
    return (
        "For each extracted field, decide whether the transcript SUPPORTS the value. "
        "Return exactly one entry for EVERY extracted field_path — never omit any — "
        "with supported (bool), 0-100 confidence, and a short evidence quote.\n\n"
        f"extracted:\n{json.dumps(items)}\n\ntranscript:\n{_turns_block(turns)}"
    )


def parse_judge_response(data: list[dict[str, Any]]) -> list[JudgeVerdict]:
    return [
        JudgeVerdict(
            field_path=str(d["field_path"]),
            supported=bool(d["supported"]),
            confidence=_clamp_confidence(d["confidence"]),
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
# Gemini drops or rewords items from a large free-form-keyed array (it truncates
# the tail on the 40+-field infertility forms), leaving those answers with no
# verdict → no FieldEvaluation → read as unsatisfied → re-asked on retry and the
# payer re-dialed. Constraining field_path to an enum of the exact asked paths
# makes a reworded verdict impossible, and judge() re-runs on the still-missing
# subset (a smaller batch the model does return in full) up to this many passes.
_JUDGE_MAX_ATTEMPTS = 3


def _judge_schema(field_paths: list[str]) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                # Gemini enforces an enum only with format "enum"; `enum` alone is advisory.
                "field_path": {"type": "string", "format": "enum", "enum": field_paths},
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

    @staticmethod
    def _loads_response(text: str | None) -> list[dict[str, Any]]:
        """Parse a JSON LLM response body.

        Raises RuntimeError on None/empty text (safety block or empty finish) so the
        caller gets a typed, descriptive error instead of a raw TypeError from json.loads.
        """
        if not text:
            raise RuntimeError("empty LLM response (finish/safety block)")
        return json.loads(text)  # type: ignore[no-any-return]

    async def _generate(self, prompt: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
        resp = await self._client.aio.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        return self._loads_response(resp.text)

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
        by_path: dict[str, JudgeVerdict] = {}
        for _ in range(_JUDGE_MAX_ATTEMPTS):
            pending = [ef for ef in extracted if ef.field_path not in by_path]
            if not pending:
                break
            data = await self._generate(
                build_judge_prompt(pending, turns),
                _judge_schema([ef.field_path for ef in pending]),
            )
            for v in parse_judge_response(data):
                by_path[v.field_path] = v
        return [by_path[ef.field_path] for ef in extracted if ef.field_path in by_path]
