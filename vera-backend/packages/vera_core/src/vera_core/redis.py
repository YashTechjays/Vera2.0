"""Async Redis client factory.

Memorystore Redis in prod (inside the BAA trust boundary); docker-compose
locally. Redis holds only opaque session tokens, cached permission codes, per-call
plan/transcript state and trace links — never plaintext PHI in a log or span
(see repo CLAUDE.md).

**Several clients per process, not one.** The control plane builds a shared client
plus dedicated ones for call-stream, notifications, worker-events and post-call; the
agent worker builds one per job for events, plan, call-stream, observer and coaching.
The split is deliberate: a consumer parked in `XREAD ... BLOCK` holds its connection
for the whole window, so it must not share a pool with request-path commands.
"""

from redis.asyncio import Redis, from_url


def create_redis(redis_url: str, *, socket_timeout: float | None = None) -> Redis:
    """Build an async Redis client. `decode_responses` returns str (we store
    JSON), so callers never deal with bytes.

    Reads are ALREADY bounded when *socket_timeout* is None: redis-py defaults it to
    5s (`redis/_defaults.py`), and derives `socket_connect_timeout` from it. On expiry
    it disconnects the connection before raising `redis.TimeoutError`, which is what
    makes it safe — never bound one of these calls with `asyncio.timeout` instead,
    because cancelling mid-command desyncs the pooled connection (see
    `observability/trace_link.py` for the full mechanism).

    Pass *socket_timeout* only to tighten or loosen that per client, and mind the
    blocking consumers: a value at or below their `block=` window makes every idle
    poll take the timeout path and reconnect.
    """
    return from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        **({} if socket_timeout is None else {"socket_timeout": socket_timeout}),
    )
