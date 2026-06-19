"""Presidio AnalyzerEngine wrapper.

Built once (model load is slow), then ``detect`` is called per turn. Presidio is
synchronous, so the async codec offloads ``detect`` to a thread. This class itself
is sync and side-effect free per call (no shared mutable state), so it's safe to
call from a thread pool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import UsSsnRecognizer

from ..config import PRESIDIO_LABEL_MAP, CodecConfig, EntityType
from .gliner_backend import load_gliner_recognizer
from .proximity_enhancer import ProximityContextEnhancer
from .recognizers import build_custom_recognizers

logger = logging.getLogger(__name__)

# Two-letter US state (+DC) abbreviations — retained per Safe Harbor, not tokenized.
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

# Role words the NER models sometimes mislabel as person names.
_NAME_STOPWORDS = {
    "patient", "provider", "member", "subscriber", "caller", "physician",
    "doctor", "agent", "representative", "rep", "nurse", "guarantor",
}


@dataclass(frozen=True)
class Detection:
    """One detected PHI span, normalized to our taxonomy."""

    entity_type: EntityType
    start: int
    end: int
    score: float
    recognizer: str
    text: str
    # Set only by the known-value matcher: the pre-minted token to use directly,
    # so the seeded record value (not an STT transcription) backs the token.
    token: str | None = None


class DetectionEngine:
    def __init__(self, config: CodecConfig | None = None) -> None:
        from ..config import DEFAULT_CONFIG

        self.config = config or DEFAULT_CONFIG
        self._custom = build_custom_recognizers()
        # The built-in SSN recognizer (with delimiters) is already in the default
        # registry; we keep an instance for the NLP-free fallback path only.
        self._ssn_fallback = UsSsnRecognizer()
        # Pure-regex recognizers for the timeout fallback (no NLP pipeline needed).
        self._regex_recognizers = [*self._custom, self._ssn_fallback]
        self._analyzer = self._build_analyzer()

    def _build_analyzer(self) -> AnalyzerEngine:
        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": self.config.spacy_model}],
            }
        )
        nlp_engine = provider.create_engine()
        analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=["en"],
            context_aware_enhancer=ProximityContextEnhancer(),
        )

        for rec in self._custom:
            analyzer.registry.add_recognizer(rec)

        if self.config.use_gliner:
            gliner = load_gliner_recognizer(self.config.gliner_model)
            if gliner is not None:
                analyzer.registry.add_recognizer(gliner)
                logger.info("GLiNER backend active: %s", self.config.gliner_model)

        return analyzer

    def detect(self, text: str) -> list[Detection]:
        """Run detection on already-normalized text. Sync; offload to a thread."""
        labels = self.config.active_presidio_labels
        # GLiNER/spaCy also emit PERSON/LOCATION which we always want when active.
        results = self._analyzer.analyze(
            text=text,
            language="en",
            entities=labels or None,
            score_threshold=self.config.min_detection_score,
        )

        return self._map_results(results, text)

    def detect_regex_only(self, text: str) -> list[Detection]:
        """Fast, NLP-free fallback used when the full pass exceeds its time budget.

        Catches the high-risk *structured* identifiers (member ID, SSN, MRN, etc.)
        so a slow/timed-out NER pass never causes those to leak untokenized. Names
        and free-text locations are missed in this mode (logged by the caller).
        """
        results = []
        for rec in self._regex_recognizers:
            results.extend(rec.analyze(text=text, entities=rec.supported_entities, nlp_artifacts=None))
        results = [r for r in results if r.score >= self.config.min_detection_score]
        return self._map_results(results, text)

    def _map_results(self, results, text: str) -> list[Detection]:
        detections: list[Detection] = []
        for r in results:
            mapped = PRESIDIO_LABEL_MAP.get(r.entity_type)
            if mapped is None or not self.config.is_active(mapped):
                continue
            span = text[r.start : r.end]
            score = float(r.score)
            # Safe Harbor retains the State; spaCy tags a 2-letter state abbrev as a
            # location -> CITY, so drop those to keep the state in the clear.
            if mapped is EntityType.CITY and span.upper() in _US_STATES:
                continue
            # GLiNER/spaCy occasionally tag role words ("patient", "provider") as names.
            if mapped is EntityType.NAME and span.lower().removeprefix("the ").strip() in _NAME_STOPWORDS:
                continue
            # spaCy tags bare integers as high-confidence DATE, but on a payer call a
            # number like 244523 / 32801 / 919912345 is really an ID / ZIP / phone, not
            # a date. Demote those so structured recognizers win the overlap, while still
            # keeping the DATE as a redaction backstop (never reduce coverage). We keep
            # 4-digit (plausible year) and 8-digit (plausible MMDDYYYY) spans as dates.
            if (
                mapped is EntityType.DATE
                and span.isdigit()
                and (len(span) in (5, 6, 7) or len(span) >= 9)
            ):
                score = min(score, self.config.min_detection_score + 0.01)
            detections.append(
                Detection(
                    entity_type=mapped,
                    start=r.start,
                    end=r.end,
                    score=score,
                    recognizer=_recognizer_name(r),
                    text=span,
                )
            )
        return detections


def _recognizer_name(result) -> str:
    meta = getattr(result, "recognition_metadata", None) or {}
    return str(meta.get("recognizer_name", "unknown"))
