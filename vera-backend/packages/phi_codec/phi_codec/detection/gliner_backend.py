"""Tier-2 ML NER backend for free-text names/locations, behind a swappable interface.

Default is Presidio's built-in ``GLiNERRecognizer`` running ``urchade/gliner_multi_pii-v1``
on CPU. The ``NerBackend`` protocol is the seam: drop in a GPU sidecar client later
without touching the engine or codec. If GLiNER (torch) isn't installed or the model
can't load, ``load_gliner_recognizer`` returns None and detection degrades to
regex + spaCy NER — logged, never silent.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# GLiNER label -> Presidio entity label. Deliberately NARROW: GLiNER only covers what
# the regex tier can't — free-text person and city names. Broader labels were removed
# because they hurt more than helped:
#   * "address"/"location"/"street" -> one giant span over the whole address, which
#     (via containment) swallowed the STREET_ADDRESS/ZIP regex hits AND the retained
#     State abbreviation — a Safe Harbor regression.
#   * "patient" -> matched the literal word "patient" as a person name.
# Structured identifiers (SSN, phone, email, dates) are handled precisely by regex.
GLINER_ENTITY_MAP: dict[str, str] = {
    "person": "PERSON",
    "person name": "PERSON",
    "city": "LOCATION",
}


def load_gliner_recognizer(model_name: str, *, map_location: str = "cpu"):
    """Build a Presidio GLiNERRecognizer, or return None if unavailable.

    Loading triggers a one-time model download from HuggingFace and is slow
    (~seconds), so build this once at startup and reuse the recognizer.
    """
    try:
        from presidio_analyzer.predefined_recognizers import GLiNERRecognizer
    except Exception as exc:  # pragma: no cover - import shape varies by version
        logger.warning("GLiNERRecognizer not importable (%s); regex+spaCy only", exc)
        return None

    try:
        return GLiNERRecognizer(
            model_name=model_name,
            entity_mapping=GLINER_ENTITY_MAP,
            flat_ner=False,
            multi_label=True,
            map_location=map_location,
        )
    except Exception as exc:
        logger.warning("GLiNER model %s failed to load (%s); regex+spaCy only", model_name, exc)
        return None
