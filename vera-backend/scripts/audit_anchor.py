"""CronJob entrypoint: anchor audit_log chain heads to the WORM bucket.

Reads each tenant chain's head via audit_chain_heads(), writes one immutable
anchor object (digests only — no PHI) to the configured sink (GCS in prod,
local filesystem in dev). Schedule/cadence is owned by the GKE CronJob, not this
script (default hourly; see the spec)."""

import asyncio

from vera_core.audit.anchor import build_anchor_sink, run_anchor
from vera_core.config import get_settings
from vera_core.db import create_engine, create_sessionmaker


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    try:
        sink = build_anchor_sink(settings)
        key = await run_anchor(sessionmaker, sink)
        print(f"anchored: {key}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
