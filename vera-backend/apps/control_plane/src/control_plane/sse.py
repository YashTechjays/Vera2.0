"""SSE framing over a keepalive-aware event stream.

The Redis stream stores yield None on every idle XREAD BLOCK window (see
`RedisCallStreamStore.read` / `RedisTranscriptStore.read`). A silent call would
otherwise send zero bytes down an open SSE response, and intermediary proxies
kill quiet connections — nginx's default `proxy_read_timeout` is 60s, well
inside a real hold-music stretch. Framing that idle tick as an SSE comment
(ignored by EventSource clients) keeps bytes flowing without inventing events.
"""

from collections.abc import AsyncIterator, Callable

SSE_KEEPALIVE_FRAME = ": keep-alive\n\n"


async def frames_with_keepalive[E](
    items: AsyncIterator[tuple[str, E] | None],
    frame: Callable[[str, E], str],
) -> AsyncIterator[str]:
    """Frame each (entry_id, event) pair with `frame`; idle ticks become comments."""
    async for item in items:
        yield SSE_KEEPALIVE_FRAME if item is None else frame(*item)
