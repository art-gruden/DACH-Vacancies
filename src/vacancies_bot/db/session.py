from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_engine(url: str) -> AsyncEngine:
    if url.startswith("sqlite"):
        engine = create_async_engine(url, connect_args={"timeout": 30})

        @event.listens_for(engine.sync_engine, "connect")
        def _fk(dbapi_conn, _):  # SQLite needs foreign keys switched on for ON DELETE CASCADE
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_async_engine(url, pool_size=10, max_overflow=5, pool_pre_ping=True)


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = make_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.is_pg = self.engine.dialect.name == "postgresql"

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as s:
            try:
                yield s
                await s.commit()
            except BaseException:
                await s.rollback()
                raise

    def insert(self, table):
        """Dialect-specific INSERT supporting ``on_conflict_do_nothing/update``."""
        return (pg_insert if self.is_pg else sqlite_insert)(table)

    async def create_all(self) -> None:
        from .models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        from .models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def try_leader_lock(self) -> Any:
        """Only one process may run the feed/sender workers. PostgreSQL: session advisory lock held on a
        dedicated connection for the life of the process. SQLite: single process by definition."""
        if not self.is_pg:
            return True
        conn = await self.engine.connect()
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(727274001)"))).scalar()
        if not got:
            await conn.close()
            return None
        return conn

    async def dispose(self) -> None:
        await self.engine.dispose()


def as_aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
