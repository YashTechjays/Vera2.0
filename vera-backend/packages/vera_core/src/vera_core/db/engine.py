"""Async engine / session factories. One engine per process, sessions per request."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vera_core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        # Recycle idle connections instead of pre-pinging: pre_ping costs a
        # SELECT 1 on every pool checkout (three checkouts per authenticated
        # request: request session + two audit sessions), while stale
        # connections only appear after long idle periods.
        pool_recycle=300,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
