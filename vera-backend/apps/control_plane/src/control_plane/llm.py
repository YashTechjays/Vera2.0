"""Vertex AI Gemini implementation of the post-call LLMClient. Structured output;
Flash by default. Consumes only the de-identified transcript — no raw PHI."""

import asyncio
import json
import logging
from itertools import batched
from typing import Any

from google import genai
from google.genai import types

from vera_core.forms.extraction_prompt import answer_shape_rules, special_values_hint
from vera_core.forms.review import is_blank_answer
from vera_core.integrations.llm import (
    ExtractedField,
    JudgeVerdict,
    LLMClient,
    SpecialValues,
    TranscriptTurn,
)

logger = logging.getLogger("control_plane.llm")


def _turns_block(turns: list[TranscriptTurn]) -> str:
    return "\n".join(f"[{t.seq}] {t.role}: {t.text}" for t in turns)


def build_extract_prompt(
    field_paths: list[str],
    turns: list[TranscriptTurn],
    special_values: SpecialValues | None = None,
) -> str:
    """The top-up prompt. `special_values` names the requested paths' authored literals: an
    extractor never shown them writes no non-numeric answer at all, so an "unlimited"
    deductible comes back empty and its gated follow-ups keep the payer on the redial list."""
    named_by_path = special_values or {}
    named = "\n".join(
        f"- {path}{hint}"
        for path in field_paths
        if (hint := special_values_hint(named_by_path.get(path)))
    )
    # A form whose requested paths name nothing gets the prompt byte-for-byte as before.
    named_block = f"named answers:\n{named}\n\n" if named else ""
    return (
        "You are extracting insurance-benefit answers from a de-identified call "
        "transcript. Turns are numbered [n]. For each requested field_path, return the "
        "value stated by the payer, a 0-100 confidence, and evidence_seq = the [n] of the "
        "turn that supports it. Omit fields not present. Do NOT invent values. "
        f"{answer_shape_rules(names_exact=bool(named))}\n\n"
        f"field_paths:\n{json.dumps(field_paths)}\n\n"
        f"{named_block}"
        f"transcript:\n{_turns_block(turns)}"
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


def build_judge_prompt(extracted: list[ExtractedField], turns_block: str) -> str:
    items = [
        {"field_path": e.field_path, "value": e.value, "evidence_seq": e.evidence_seq}
        for e in extracted
    ]
    return (
        "For each extracted field, decide whether the transcript SUPPORTS the value. "
        "Return exactly one entry for EVERY extracted field_path — never omit any — "
        "with supported (bool), 0-100 confidence, and a short evidence quote.\n\n"
        f"extracted:\n{json.dumps(items)}\n\ntranscript:\n{turns_block}"
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
# A full infertility form judges ~180 paths; a 180-value enum can exceed Vertex's
# schema limit and 400 the whole call. Chunk to bound the enum AND the batch the
# model must return in full (smaller batches drop fewer items).
_JUDGE_CHUNK_SIZE = 50
# Errored chunks retry after this pause — concurrent chunks fail together (429
# bursts), and an instant re-fire lands in the same exhausted quota window.
_JUDGE_RETRY_BACKOFF_S = 2.0


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
    def __init__(
        self,
        *,
        project: str,
        location: str,
        model: str,
        timeout_ms: int = 120_000,
        max_concurrency: int = 8,
    ) -> None:
        self._client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
        self._model = model
        # One shared client serves every post-call job, so this caps process-wide
        # Vertex fan-out (the consumer gathers up to 16 jobs, each with several chunks).
        self._semaphore = asyncio.Semaphore(max_concurrency)

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
        async with self._semaphore:
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
        self,
        *,
        field_paths: list[str],
        turns: list[TranscriptTurn],
        special_values: SpecialValues | None = None,
    ) -> list[ExtractedField]:
        data = await self._generate(
            build_extract_prompt(field_paths, turns, special_values), _EXTRACT_SCHEMA
        )
        return parse_extract_response(data)

    async def _judge_chunk(
        self, chunk: tuple[ExtractedField, ...], turns_block: str
    ) -> list[JudgeVerdict]:
        chunk_paths = [ef.field_path for ef in chunk]
        data = await self._generate(
            build_judge_prompt(list(chunk), turns_block), _judge_schema(chunk_paths)
        )
        # ignore reworded/stray paths
        return [v for v in parse_judge_response(data) if v.field_path in chunk_paths]

    async def judge(
        self, *, extracted: list[ExtractedField], turns: list[TranscriptTurn]
    ) -> list[JudgeVerdict]:
        if not extracted:
            return []
        by_path: dict[str, JudgeVerdict] = {}
        last_error: Exception | None = None
        turns_block = _turns_block(turns)
        errored = False
        for _ in range(_JUDGE_MAX_ATTEMPTS):
            pending = [ef for ef in extracted if ef.field_path not in by_path]
            if not pending:
                break
            if errored:
                await asyncio.sleep(_JUDGE_RETRY_BACKOFF_S)
            chunks = list(batched(pending, _JUDGE_CHUNK_SIZE))
            # gather, not TaskGroup: a failed chunk must not cancel its siblings (salvage contract).
            results = await asyncio.gather(
                *(self._judge_chunk(chunk, turns_block) for chunk in chunks),
                return_exceptions=True,
            )
            errored = any(isinstance(r, Exception) for r in results)
            progressed = False
            for chunk, result in zip(chunks, results, strict=True):
                if isinstance(result, BaseException):
                    if not isinstance(result, Exception):
                        raise result  # CancelledError and friends must propagate
                    last_error = result
                    logger.warning(
                        "judge: chunk of %d field(s) failed (%s) — salvaging remaining verdicts",
                        len(chunk),
                        type(result).__name__,
                    )
                    continue
                for v in result:
                    by_path[v.field_path] = v
                    progressed = True
            # Errors are transient-shaped, so an errored attempt keeps its retries;
            # a clean attempt that returned nothing new won't improve on a re-ask.
            if not progressed and not errored:
                break
        # An error that left ANY field unjudged must surface so the caller routes to
        # LLM_ERROR review — otherwise those fields look unsatisfied and the payer is
        # redialed for data a transient error merely failed to score. A retry that
        # recovered full coverage clears this (nothing left uncovered).
        if last_error is not None and any(ef.field_path not in by_path for ef in extracted):
            raise last_error
        return [by_path[ef.field_path] for ef in extracted if ef.field_path in by_path]
