"""Cross-process trace join for one call.

Langfuse rolls cost up reliably per TRACE; its SESSION rollup renders $0.00 when cost
is model-calculated rather than caller-ingested (langfuse#15109), which is exactly
Vera's case since prices live in Langfuse. So a per-call total is only real if every
span belonging to the call shares ONE trace id — including the ones the control plane
emits minutes later (post-call eval, summary) or mid-call from a browser request
(coaching whisper).

The worker cannot derive that id: LiveKit mints the `job_entrypoint` span before
Vera's code runs, and every auto-instrumented STT/LLM/TTS span hangs beneath it. A
deterministic id computed from the room name would create a SECOND trace alongside
it. So the id is propagated, not derived: the worker publishes its W3C traceparent
once per call, keyed by room name, and the control plane adopts it as a remote parent.

Every operation is best-effort. A missing or unusable link degrades to a root span —
the work still traces, it just forms its own trace — and never raises into a call or
an HTTP request.

PHI: a traceparent is random hex identifiers only.
"""

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes

from vera_core.observability.correlation import call_trace_attributes

logger = logging.getLogger("vera.observability")

# Sized for the longest post-call window, not the call: post-call eval normally runs
# seconds after call.ended, but the pipeline sweeper can re-drive a stranded job much
# later. One small key per call is negligible.
TRACE_LINK_TTL_SECONDS = 24 * 60 * 60

_PROPAGATOR = TraceContextTextMapPropagator()
_TRACEPARENT = "traceparent"


def trace_link_key(room_name: str) -> str:
    """Per-call Redis key, following the `vera:<thing>:<room>` convention."""
    return f"vera:trace:{room_name}"


def current_traceparent() -> str | None:
    """The ambient OTel context as a W3C traceparent, or None when tracing is a no-op.

    MUST be called where the intended parent span is genuinely ambient — in the agent
    worker that is the job entrypoint, where LiveKit's `job_entrypoint` span is active.
    """
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get(_TRACEPARENT)


def remote_parent(traceparent: str | None) -> Context | None:
    """A Context parented at *traceparent*, or None when it is absent or unusable.

    None is the graceful-degradation signal meaning "open a root span instead".
    """
    if not traceparent:
        return None
    ctx = _PROPAGATOR.extract({_TRACEPARENT: traceparent})
    if not trace.get_current_span(ctx).get_span_context().is_valid:
        return None
    return ctx


class TraceLinkStore:
    """Publishes and resolves a call's traceparent over Redis.

    Both methods swallow every Redis failure — including a timeout — so an
    observability outage can never affect a call or an API request.

    Bounding the wait is left to the CLIENT, which already does it: redis-py defaults
    `socket_timeout` to 5s (`redis/_defaults.py`), and on expiry it disconnects the
    connection itself before raising `redis.TimeoutError`. Never wrap these calls in
    `asyncio.timeout` to tighten that: cancelling mid-command leaves the sent command's
    reply unread in the socket, and `Redis.execute_command` returns the connection to
    the pool anyway (its `except Exception` does not catch `CancelledError`, and
    `pool.release` only disconnects when `should_reconnect()` — a flag nothing on this
    path sets). The next caller to take that connection then reads the previous
    command's reply. A tighter bound belongs in `create_redis(socket_timeout=...)`,
    where redis-py can clean up after itself.
    """

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def publish(self, room_name: str, traceparent: str) -> None:
        try:
            await self._redis.set(trace_link_key(room_name), traceparent, ex=TRACE_LINK_TTL_SECONDS)
        except Exception as exc:
            logger.warning("trace link publish failed: %s", type(exc).__name__)

    async def resolve(self, room_name: str) -> Context | None:
        try:
            raw = await self._redis.get(trace_link_key(room_name))
            if raw is not None:
                return remote_parent(raw.decode() if isinstance(raw, bytes) else str(raw))
        except Exception as exc:
            logger.warning("trace link resolve failed: %s", type(exc).__name__)
        return None


@contextmanager
def phi_safe_span(
    tracer: Tracer,
    name: str,
    *,
    attributes: Attributes = None,
    context: Context | None = None,
) -> Iterator[Span]:
    """`start_as_current_span` with exception recording held OFF.

    Use this for ANY span that wraps a provider call, whether or not it belongs to a
    call. A provider error can embed the request payload — a transcript, a supervisor's
    audio, an extracted answer — and `record_exception` copies that message into a span
    event while `set_status_on_exception` copies it into the status description. Both
    then leave the trust boundary on export. Attach no prompt, response or error text
    to the yielded span either; the exporter ships every attribute.

    The default is the wrong way round for Vera, so the safe posture lives in one place
    rather than as two keywords each call site has to remember.
    """
    with tracer.start_as_current_span(
        name,
        context=context,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        yield span


@asynccontextmanager
async def call_scoped_span(
    tracer: Tracer,
    name: str,
    *,
    room_name: str,
    trace_links: TraceLinkStore | None,
) -> AsyncIterator[None]:
    """A `phi_safe_span` parented into the call's OWN trace, degrading to a root span
    when the link is missing or expired.

    The parent comes from the worker's published traceparent because Langfuse's
    per-SESSION cost rollup renders $0.00 for model-calculated cost (langfuse#15109),
    so per-TRACE is the only unit that makes a per-call total real.
    """
    parent = await trace_links.resolve(room_name) if trace_links is not None else None
    attributes = call_trace_attributes(room_name, in_call_trace=parent is not None)
    with phi_safe_span(tracer, name, attributes=attributes, context=parent):
        yield
