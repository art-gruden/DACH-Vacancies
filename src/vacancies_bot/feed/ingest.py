"""Durable ingestion of the crawler's change feed (``/changes``, ``/changes/head``).

* First start: the cursor is taken from ``/changes/head`` — the bot follows changes from now on and never
  replays the crawler's history as "new".
* Every page is stored in ``crawler_event_inbox`` (``ON CONFLICT DO NOTHING`` on ``event_id``) **in the same
  transaction** that advances the cursor. A crash before COMMIT re-reads the same page (the crawler guarantees
  replayability); a crash after COMMIT has both. The cursor never moves past events that are not stored.
* 400 (invalid cursor) / 409 (cursor ahead of the log, e.g. crawler DB restored): restart from ``/changes/head``
  and flag a re-sync; the processor then catches up per subscription with ``/jobs?new_since=…``.
"""

from __future__ import annotations

import time

from sqlalchemy import func, select

from ..crawler.client import CrawlerClient, CrawlerError, CursorAheadError, InvalidCursorError
from ..db.models import CrawlerEventInbox, CrawlerSyncCursor
from ..db.session import Database, utcnow
from ..logging import get_logger

log = get_logger(__name__)
NAME = "changes"


class FeedIngestor:
    def __init__(self, db: Database, client: CrawlerClient, *, page_size: int = 200) -> None:
        self.db = db
        self.client = client
        self.page_size = page_size

    async def _cursor(self) -> CrawlerSyncCursor:
        async with self.db.session() as s:
            row = await s.get(CrawlerSyncCursor, NAME)
            if row is not None:
                return row
        head = await self.client.changes_head()
        async with self.db.session() as s:
            stmt = self.db.insert(CrawlerSyncCursor).values(name=NAME, cursor=head, last_event_id=0, resyncs=0, updated_at=utcnow())
            await s.execute(stmt.on_conflict_do_nothing(index_elements=["name"]))
        log.info("feed_cursor_initialised_from_head")
        async with self.db.session() as s:
            return await s.get(CrawlerSyncCursor, NAME)

    async def last_event_id(self) -> int:
        async with self.db.session() as s:
            row = await s.get(CrawlerSyncCursor, NAME)
            inbox_max = (await s.execute(select(func.max(CrawlerEventInbox.event_id)))).scalar() or 0
            return max(row.last_event_id if row else 0, inbox_max)

    async def poll(self, *, max_pages: int = 20, budget_s: float | None = None) -> int:
        """Read pages until the feed is drained (or limits hit). Returns the number of new events stored."""
        started = time.monotonic()
        row = await self._cursor()
        cursor = row.cursor
        stored = 0
        for _ in range(max_pages):
            try:
                page = await self.client.changes(cursor, limit=self.page_size)
            except (CursorAheadError, InvalidCursorError) as exc:
                await self._reset(type(exc).__name__)
                return stored
            new_last = max((e.event_id for e in page.items), default=0)
            async with self.db.session() as s:
                if page.items:
                    rows = [
                        {"event_id": e.event_id, "event_type": e.event_type, "job_id": e.job_id, "occurred_at": e.occurred_at,
                         "fields": e.fields, "reason": e.reason, "status": "pending", "attempts": 0, "ingested_at": utcnow()}
                        for e in page.items
                    ]
                    res = await s.execute(self.db.insert(CrawlerEventInbox).values(rows).on_conflict_do_nothing(index_elements=["event_id"]))
                    stored += max(res.rowcount or 0, 0)
                cur = await s.get(CrawlerSyncCursor, NAME, with_for_update=self.db.is_pg)
                cur.cursor = page.next_cursor
                cur.last_event_id = max(cur.last_event_id, new_last)
                cur.last_poll_ok_at = utcnow()
            cursor = page.next_cursor
            if not page.has_more:
                break
            if budget_s is not None and time.monotonic() - started > budget_s:
                break
        if stored:
            log.info("feed_ingested", events=stored)
        return stored

    async def _reset(self, why: str) -> None:
        head = await self.client.changes_head()
        async with self.db.session() as s:
            cur = await s.get(CrawlerSyncCursor, NAME)
            since = cur.last_poll_ok_at or utcnow()
            cur.cursor = head
            cur.resyncs += 1
            cur.resync_since = cur.resync_since or since
        log.error("feed_cursor_reset", reason=why)

    async def catch_up(self, budget_s: float = 8.0) -> int:
        """Drain the feed before a preview so that the baseline is current. Returns the baseline event id.
        On crawler errors the stored position is used (matches are de-duplicated anyway)."""
        try:
            await self.poll(max_pages=50, budget_s=budget_s)
        except CrawlerError as exc:
            log.warning("feed_catch_up_failed", error=type(exc).__name__)
        return await self.last_event_id()
