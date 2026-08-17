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
from opentelemetry import trace
from opentelemetry.context import Context

from vera_core.observability.correlation import call_trace_attributes

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
# Emitted only through `llm_token_usage` below, which the post-call eval (llm.py) and
# the export-time LLM correction (llm_usage_export.py) both go through — so the string
# the seeded prices must match exactly exists in exactly one place.
USAGE_CACHED = "cached"

type UsageAttributes = dict[str, str | int | float | bool]

_tracer = trace.get_tracer("vera.observability.usage")


def llm_token_usage(*, prompt: int, cached: int, output: int) -> dict[str, int]:
    """LLM token counts as usage keys, zeros omitted.

    `input` is reduced by *cached* because every provider's prompt count INCLUDES the
    cached tokens — sending both whole double-counts the hits — so `input + cached`
    always reconstructs the original prompt count. Zeros are omitted because a
    zero-valued key would demand a price entry for a unit nobody bills.
    """
    details: dict[str, int] = {}
    if prompt - cached:
        details[USAGE_INPUT] = prompt - cached
    if cached:
        details[USAGE_CACHED] = cached
    if output:
        details[USAGE_OUTPUT] = output
    return details


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


def _emit_usage_span(
    metrics: Any,
    *,
    parent_context: Context | None,
    room_name: str | None,
    source: str | None,
) -> None:
    attrs = usage_span_attributes(metrics)
    if attrs is None:
        return
    if room_name is not None:
        attrs.update(call_trace_attributes(room_name))
    if source is not None:
        attrs["vera.usage.source"] = source
    name = SPAN_TTS_USAGE if isinstance(metrics, TTSMetrics) else SPAN_STT_USAGE
    # A point-in-time observation: every attribute is known up front, so open and close
    # immediately. `context=None` means "use the ambient context", which is what the
    # per-request coaching path needs; the worker paths pass an explicitly captured one.
    _tracer.start_span(name, context=parent_context, attributes=attrs).end()


def attach_usage_spans(
    emitter: Any,
    *,
    parent_context: Context | None = None,
    room_name: str | None = None,
    source: str | None = None,
) -> None:
    """Emit one usage generation per billable `metrics_collected` event from *emitter*.

    *emitter* is any livekit `rtc.EventEmitter` that emits `metrics_collected` — a
    plugin STT/TTS instance or an STT `FallbackAdapter` (which re-emits its inner
    metrics verbatim, so the model name stays the real provider's).

    *parent_context* MUST be captured where the intended parent span is genuinely
    ambient. In the agent worker that is the job entrypoint: the takeover STT is also
    reached from a room-event callback whose task does not carry the entrypoint's
    context, so capturing at emit time would silently produce a new trace root. Leave
    it None only when the intended parent is per-invocation (the coaching-whisper
    request span), where ambient context is the correct answer.

    Listens on the component-level emitter, NOT `session.on("metrics_collected")` —
    the latter is deprecated in livekit-agents 1.6.7 and warns per registration.
    """

    def _on_metrics(metrics: Any) -> None:
        try:
            _emit_usage_span(
                metrics, parent_context=parent_context, room_name=room_name, source=source
            )
        except Exception as exc:
            # Never let a tracing failure reach the call, the transcript, or the request.
            # Type name only: a provider error can embed the request payload.
            logger.warning("usage span emit failed: %s", type(exc).__name__)

    try:
        emitter.on("metrics_collected", _on_metrics)
    except Exception as exc:
        logger.warning("usage span attach failed: %s", type(exc).__name__)
