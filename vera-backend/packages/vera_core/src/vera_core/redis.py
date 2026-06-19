"""Async Redis client factory.

Memorystore Redis in prod (inside the BAA trust boundary); docker-compose
locally. One client per process, created in the app lifespan and closed on
shutdown. Redis holds only opaque session tokens and cached permission codes —
never plaintext PHI (see repo CLAUDE.md).
"""

from redis.asyncio import Redis, from_url


def create_redis(redis_url: str) -> Redis:
    """Build an async Redis client. `decode_responses` returns str (we store
    JSON), so callers never deal with bytes."""
    return from_url(redis_url, encoding="utf-8", decode_responses=True)
