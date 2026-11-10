"""Background loops: feed ingestion, event processing, notification sending, housekeeping.

Run inside the bot process (one asyncio loop). With PostgreSQL an advisory lock makes sure only one process
runs them, so the bot can be scaled for polling/webhooks without double processing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from .bot.context import AppContext
from .crawler.client import CrawlerAuthError, CrawlerDisabled, CrawlerError
from .logging import get_logger
from .notify.sender import Messenger, Sender

log = get_logger(__name__)


async def _loop(name: str, interval: float, fn: Callable[[], Awaitable], stop: asyncio.Event) -> None:
    backoff = interval
    while not stop.is_set():
        started = time.monotonic()
        try:
            await fn()
            backoff = interval
        except (CrawlerAuthError, CrawlerDisabled) as exc:
            backoff = max(300.0, interval)
            log.error("worker_crawler_access", worker=name, error=str(exc))
        except CrawlerError as exc:
            backoff = min(600.0, backoff * 2)
            log.warning("worker_crawler_error", worker=name, error=type(exc).__name__)
        except Exception:
            backoff = min(600.0, backoff * 2)
            log.exception("worker_failed", worker=name)
        wait = max(1.0, backoff - (time.monotonic() - started))
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except TimeoutError:
            pass


async def run_workers(ctx: AppContext, messenger: Messenger, stop: asyncio.Event) -> None:
    lock = None
    while not stop.is_set():
        lock = await ctx.db.try_leader_lock()
        if lock:
            break
        log.info("workers_waiting_for_leader_lock")
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            pass
    if stop.is_set():
        return
    st = ctx.settings
    sender = Sender(ctx.db, messenger, digest_threshold=st.digest_threshold, lease_s=st.send_lease_s, morning_hour=st.morning_digest_hour)

    async def housekeeping() -> None:
        await ctx.cv.purge_expired()
        await ctx.users.purge_old_sessions()
        await ctx.processor.purge()

    log.info("workers_started")
    try:
        await asyncio.gather(
            _loop("feed", st.feed_poll_interval_s, ctx.ingestor.poll, stop),
            _loop("processor", st.processor_interval_s, ctx.processor.run_once, stop),
            _loop("sender", st.sender_interval_s, sender.run_once, stop),
            _loop("housekeeping", 3600, housekeeping, stop),
        )
    finally:
        if lock is not True and lock is not None:
            await lock.close()
