"""Per-request STT/TTS usage as Langfuse-priceable generations.

LiveKit reports STT usage ONLY through the OTel *Metrics* API (a no-op meter here,
since `otel.py` installs a TracerProvider and nothing else) and Langfuse ingests
traces only — so STT usage reaches Langfuse nowhere. TTS does get a span, but its
usage rides a custom `lk.tts_metrics` bag Langfuse's cost engine does not read.

Both are fixed the same way: one short Vera-owned observation per
`metrics_collected` event, typed `generation` and carrying
`langfuse.observation.usage_details`. Langfuse parses that attribute with ARBITRARY
usage keys and matches each against a model price entry, so non-token billing units
price exactly like tokens. Vera holds no rates — see `scripts/seed_langfuse_prices.py`.

Two contracts this module must not break:

- **Only `generation` and `embedding` observations carry cost.** A plain span ingests
  cleanly and renders BLANK cost, which looks identical to broken instrumentation.
- **Usage values must be integers.** Langfuse stores them as `Map(String, UInt64)`;
  a float is truncated on the OTel route and dropped outright on the SDK route. Audio
  is therefore reported in whole milliseconds, rounded here rather than in ingestion.

PHI: neither STTMetrics nor TTSMetrics carries any text field (`characters_count` is
`len(input_text)`, a length). Every attribute below is a count, duration, boolean,
closed enum, or a model name Vera itself passed in. Never add the SDK's
`metrics.model_dump_json()` blob here — it carries no PHI today but would attach
whatever fields a future SDK version adds, sight unseen.
"""

import json
import logging
from typing import Any

from livekit.agents.metrics import STTMetrics, TTSMetrics

logger = logging.getLogger("vera.observability")

SPAN_STT_USAGE = "vera.stt.usage"
SPAN_TTS_USAGE = "vera.tts.usage"

# Langfuse's own namespace, which takes precedence over the gen_ai.* conventions.
OBSERVATION_TYPE_ATTR = "langfuse.observation.type"
OBSERVATION_MODEL_ATTR = "langfuse.observation.model.name"
USAGE_DETAILS_ATTR = "langfuse.observation.usage_details"

# Set explicitly rather than relying on Langfuse's implicit "a span with a model
# attribute becomes a generation" promotion — an inference rule that has changed once
# already, and cost silently disappears when it changes again.
GENERATION = "generation"

# Usage keys. These strings are a contract with the seeded Langfuse price entries —
# changing one here without changing it there silently zeroes cost.
STT_AUDIO_MS = "stt_audio_ms"
TTS_CHARACTERS = "tts_characters"
USAGE_INPUT = "input"
USAGE_OUTPUT = "output"
# Defined here with its siblings, not at each use site: the post-call eval (llm.py)
# and the export-time LLM correction (llm_usage_export.py) both emit this key, and two
# copies of a string the seeded prices must match exactly is a drift waiting to happen.
USAGE_CACHED = "cached"

type UsageAttributes = dict[str, str | int | float | bool]


def _billable_tokens(metrics: STTMetrics | TTSMetrics) -> dict[str, int]:
    """Token counts, included only when non-zero: Deepgram and Cartesia both report 0,
    and a zero-valued key would demand a price entry for a unit nobody bills. The key
    names match the LLM vocabulary so one model entry can price both."""
    tokens: dict[str, int] = {}
    if metrics.input_tokens:
        tokens[USAGE_INPUT] = metrics.input_tokens
    if metrics.output_tokens:
        tokens[USAGE_OUTPUT] = metrics.output_tokens
    return tokens


def usage_span_attributes(metrics: Any) -> UsageAttributes | None:
    """Generation attributes for one metrics event, or None when nothing is billable.

    None covers three real cases: `_report_connection_acquired`'s zero-usage
    connection-timing event (`stt.py:439`), audio too short to round to a
    millisecond, and any metrics type that is not STT/TTS (VAD/EOU cost nothing).
    """
    usage: dict[str, int]
    attrs: UsageAttributes
    if isinstance(metrics, TTSMetrics):
        usage = _billable_tokens(metrics)
        if metrics.characters_count:
            usage[TTS_CHARACTERS] = metrics.characters_count
        if not usage:
            return None
        attrs = {
            "vera.usage.streamed": metrics.streamed,
            "vera.usage.cancelled": metrics.cancelled,
            # Operational only — Cartesia bills characters, not output duration.
            "vera.usage.audio_seconds": metrics.audio_duration,
        }
    elif isinstance(metrics, STTMetrics):
        usage = _billable_tokens(metrics)
        if audio_ms := round(metrics.audio_duration * 1000):
            usage[STT_AUDIO_MS] = audio_ms
        if not usage:
            return None
        attrs = {"vera.usage.streamed": metrics.streamed}
    else:
        return None

    attrs[OBSERVATION_TYPE_ATTR] = GENERATION
    attrs[USAGE_DETAILS_ATTR] = json.dumps(usage)
    if (meta := metrics.metadata) is not None:
        if meta.model_name:
            attrs[OBSERVATION_MODEL_ATTR] = meta.model_name
            attrs["gen_ai.request.model"] = meta.model_name
        if meta.model_provider:
            attrs["gen_ai.provider.name"] = meta.model_provider
    return attrs
