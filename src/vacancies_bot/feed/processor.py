"""Evaluates ingested change events against subscriptions and queues notifications.

Model: every subscription keeps a *watermark* (the last event id it has been evaluated against). The global
watermark is the highest event id up to which every inbox event is *ready* (vacancy detail fetched or not
needed). Each run evaluates, per subscription, the events in ``(sub.watermark, global]`` and moves its
watermark in the **same transaction** as the matches and outbox rows it creates. Consequences:

* a crash at any point repeats work idempotently (unique ``(subscription_id, job_id)`` and outbox ``dedup_key``);
* a new subscription starts at the baseline captured before its initial search, so events that happened
  between preview and activation are evaluated too, and vacancies already shown in the initial results are
  stored as ``origin=initial`` matches and never re-sent as "new";
* events are evaluated in commit order; a vacancy detail that cannot be fetched blocks only until it succeeds
  or exceeds ``event_max_attempts`` (then the event is ``skipped`` and logged).

``job.updated`` never creates a "new vacancy" message by itself; it re-scores existing matches and notifies
only when the subscription opted in (``notify_updates``). ``job.closed`` updates saved vacancies and cancels
pending notifications, never sends a message.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import and_, delete, func, select, update

from ..crawler.client import CrawlerClient, CrawlerError
from ..crawler.models import JobDetail
from ..db.models import (
    BotUser,
    CrawlerEventInbox,
    CrawlerSyncCursor,
    JobCache,
    NotificationOutbox,
    SavedJob,
    Subscription,
    SubscriptionMatch,
)
from ..db.session import Database, as_aware, utcnow
from ..domain.criteria import SearchCriteria
from ..domain.matching import SCORER_VERSION, score_job
from ..domain.query_builder import build_queries
from ..logging import get_logger
from ..notify.planner import due_at

log = get_logger(__name__)
DETAIL_EVENTS = ("job.created", "job.updated", "job.reopened")
MEANINGFUL_UPDATE_FIELDS = {"title", "salary", "work_model", "locations", "employment", "remote_eligibility", "languages", "apply_url", "description"}


def content_hash(job: JobDetail) -> str:
    text = json.dumps([job.title, job.description or "", [s.name for s in job.skills]], ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


class FeedProcessor:
    def __init__(self, db: Database, client: CrawlerClient, *, max_attempts: int = 8, morning_hour: int = 8,
                 evening_hour: int = 19, retention_days: int = 14) -> None:
        self.db = db
        self.client = client
        self.max_attempts = max_attempts
        self.morning_hour = morning_hour
        self.evening_hour = evening_hour
        self.retention_days = retention_days

    # --- step 1: make events ready --------------------------------------------------------------------------
    async def prepare(self, limit: int = 300) -> int:
        now = utcnow()
        async with self.db.session() as s:
            events = list((await s.execute(
                select(CrawlerEventInbox).where(CrawlerEventInbox.status == "pending").order_by(CrawlerEventInbox.event_id).limit(limit)
            )).scalars())
        events = [e for e in events if e.next_attempt_at is None or as_aware(e.next_attempt_at) <= now]
        if not events:
            return 0
        need = sorted({e.job_id for e in events if e.event_type in DETAIL_EVENTS})
        details: dict[int, JobDetail | None] = {}
        failures: dict[int, str] = {}
        for job_id in need:
            try:
                details[job_id] = await self.client.get_job(job_id)
            except CrawlerError as exc:
                failures[job_id] = f"{type(exc).__name__}: {exc}"[:400]
        ready = 0
        async with self.db.session() as s:
            for job_id, job in details.items():
                if job is None:
                    continue
                payload = job.model_dump(mode="json")
                values = {"payload": payload, "status": job.status, "crawler_updated_at": job.updated_at.isoformat(),
                          "content_hash": content_hash(job), "fetched_at": now}
                stmt = self.db.insert(JobCache).values(job_id=job_id, **values)
                await s.execute(stmt.on_conflict_do_update(index_elements=["job_id"], set_=values))
            for e in events:
                ev = await s.get(CrawlerEventInbox, e.event_id)
                if ev.event_type in DETAIL_EVENTS and ev.job_id in failures:
                    ev.attempts += 1
                    ev.last_error = failures[ev.job_id]
                    if ev.attempts >= self.max_attempts:
                        ev.status = "skipped"
                        log.error("feed_event_skipped", event_id=ev.event_id, job_id=ev.job_id, error=ev.last_error)
                    else:
                        ev.next_attempt_at = now + timedelta(seconds=min(3600, 30 * 2 ** ev.attempts))
                    continue
                if ev.event_type in ("job.closed",):
                    await self._apply_closed(s, ev.job_id)
                ev.status = "ready"
                ev.ready_at = now
                ready += 1
        return ready

    async def _apply_closed(self, s, job_id: int) -> None:
        await s.execute(update(JobCache).where(JobCache.job_id == job_id).values(status="closed"))
        await s.execute(update(SavedJob).where(SavedJob.job_id == job_id).values(job_closed=True))
        await s.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.job_id == job_id, NotificationOutbox.status == "pending")
            .values(status="cancelled", last_error="job closed")
        )

    async def global_watermark(self) -> int:
        async with self.db.session() as s:
            first_pending = (await s.execute(
                select(func.min(CrawlerEventInbox.event_id)).where(CrawlerEventInbox.status == "pending")
            )).scalar()
            if first_pending is not None:
                return int(first_pending) - 1
            return int((await s.execute(select(func.max(CrawlerEventInbox.event_id)))).scalar() or 0)

    # --- step 2: evaluate per subscription ---------------------------------------------------------------------------
    async def evaluate(self) -> int:
        upto = await self.global_watermark()
        async with self.db.session() as s:
            subs = list((await s.execute(
                select(Subscription.id).where(Subscription.enabled.is_(True), Subscription.watermark_event_id < upto)
                .order_by(Subscription.id)
            )).scalars())
        queued = 0
        for sid in subs:
            queued += await self._evaluate_sub(sid, upto)
        return queued

    async def _evaluate_sub(self, sub_id: int, upto: int) -> int:
        queued = 0
        now = utcnow()
        async with self.db.session() as s:
            sub = await s.get(Subscription, sub_id, with_for_update=self.db.is_pg)
            if sub is None or not sub.enabled or sub.watermark_event_id >= upto:
                return 0
            user = await s.get(BotUser, sub.user_id)
            criteria = SearchCriteria.model_validate(sub.criteria)
            events = list((await s.execute(
                select(CrawlerEventInbox).where(
                    CrawlerEventInbox.event_id > sub.watermark_event_id,
                    CrawlerEventInbox.event_id <= upto,
                    CrawlerEventInbox.status == "ready",
                    CrawlerEventInbox.event_type.in_(DETAIL_EVENTS),
                ).order_by(CrawlerEventInbox.event_id)
            )).scalars())
            jobs = {}
            if events:
                rows = (await s.execute(select(JobCache).where(JobCache.job_id.in_({e.job_id for e in events})))).scalars()
                jobs = {r.job_id: r for r in rows}
            for ev in events:
                cache = jobs.get(ev.job_id)
                if cache is None or cache.status != "open":
                    continue
                job = JobDetail.model_validate(cache.payload)
                existing = (await s.execute(
                    select(SubscriptionMatch).where(SubscriptionMatch.subscription_id == sub.id, SubscriptionMatch.job_id == ev.job_id)
                )).scalar_one_or_none()
                if ev.event_type == "job.updated":
                    if existing is not None:
                        old_ok = existing.passed and existing.score >= criteria.min_score
                        m = score_job(job, criteria, multipliers=sub.weights or None)
                        existing.score, existing.passed, existing.data = m.score, m.passed, m.to_json()
                        existing.job_revision = cache.crawler_updated_at
                        existing.evaluated_at = now
                        meaningful = bool(set(ev.fields or []) & MEANINGFUL_UPDATE_FIELDS)
                        if sub.notify_updates and existing.notified and meaningful and m.passed and m.score >= criteria.min_score and old_ok:
                            queued += await self._enqueue(s, user, sub, job.id, m.score, "update", now, extra={"fields": ev.fields or []},
                                                          dedup=f"upd:{user.id}:{job.id}:{ev.event_id}")
                        continue
                    if not sub.notify_updates:
                        continue
                if existing is not None:
                    if ev.event_type == "job.reopened" and existing.notified and sub.notify_updates:
                        queued += await self._enqueue(s, user, sub, job.id, existing.score, "reopened", now, dedup=f"reo:{user.id}:{job.id}:{ev.event_id}")
                    continue
                m = score_job(job, criteria, multipliers=sub.weights or None)
                notify = m.passed and m.score >= criteria.min_score
                s.add(SubscriptionMatch(
                    subscription_id=sub.id, job_id=job.id, job_revision=cache.crawler_updated_at, score=m.score, passed=m.passed,
                    data=m.to_json(), origin="reopened" if ev.event_type == "job.reopened" else "feed", notified=notify and sub.notification_mode != "MANUAL",
                    model_version=SCORER_VERSION, evaluated_at=now,
                ))
                await s.flush()
                if notify:
                    queued += await self._enqueue(s, user, sub, job.id, m.score, "new", now)
            sub.watermark_event_id = upto
        return queued

    async def _enqueue(self, s, user: BotUser, sub: Subscription, job_id: int, score: int, kind: str, now, *,
                       extra: dict | None = None, dedup: str | None = None) -> int:
        when = due_at(now, sub.notification_mode, tz=user.timezone, quiet_start=user.quiet_start, quiet_end=user.quiet_end,
                      morning_hour=self.morning_hour, evening_hour=self.evening_hour)
        if when is None or user.blocked_at is not None:
            return 0
        key = dedup or f"new:{user.id}:{job_id}"
        existing = (await s.execute(select(NotificationOutbox).where(NotificationOutbox.dedup_key == key))).scalar_one_or_none()
        if existing is not None:
            # the same vacancy from several subscriptions of one user → one message listing all of them
            if existing.status == "pending" and sub.id not in existing.subscription_ids:
                existing.subscription_ids = [*existing.subscription_ids, sub.id]
                existing.payload = {**existing.payload, "scores": {**existing.payload.get("scores", {}), str(sub.id): score}}
                if when < as_aware(existing.due_at):
                    existing.due_at = when
            return 0
        s.add(NotificationOutbox(
            user_id=user.id, job_id=job_id, kind=kind, dedup_key=key, subscription_ids=[sub.id],
            payload={"scores": {str(sub.id): score}, **(extra or {}), "digest": sub.notification_mode in ("MORNING", "EVENING")},
            status="pending", due_at=when, created_at=now,
        ))
        await s.flush()
        return 1

    # --- step 3: re-sync after the cursor had to be reset --------------------------------------------------------------
    async def resync_if_needed(self, per_query: int = 50) -> int:
        async with self.db.session() as s:
            cur = await s.get(CrawlerSyncCursor, "changes")
            since = as_aware(cur.resync_since) if cur else None
        if since is None:
            return 0
        queued = 0
        async with self.db.session() as s:
            subs = list((await s.execute(select(Subscription).where(Subscription.enabled.is_(True)))).scalars())
        for sub in subs:
            criteria = SearchCriteria.model_validate(sub.criteria)
            found: dict[int, object] = {}
            for q in build_queries(criteria):
                q.new_since = since
                try:
                    page = await self.client.search_jobs(q, limit=per_query)
                except CrawlerError as exc:
                    log.warning("resync_query_failed", sub=sub.id, error=type(exc).__name__)
                    return queued  # keep the flag; retry next run
                for j in page.items:
                    found.setdefault(j.id, j)
            now = utcnow()
            async with self.db.session() as s:
                fresh = await s.get(Subscription, sub.id)
                user = await s.get(BotUser, fresh.user_id)
                for job in found.values():
                    exists = (await s.execute(select(SubscriptionMatch.id).where(
                        and_(SubscriptionMatch.subscription_id == sub.id, SubscriptionMatch.job_id == job.id)))).scalar()
                    if exists:
                        continue
                    m = score_job(job, criteria, multipliers=fresh.weights or None)
                    notify = m.passed and m.score >= criteria.min_score
                    s.add(SubscriptionMatch(subscription_id=sub.id, job_id=job.id, job_revision=job.updated_at.isoformat(), score=m.score,
                                            passed=m.passed, data=m.to_json(), origin="feed", notified=notify, model_version=SCORER_VERSION,
                                            evaluated_at=now))
                    if notify:
                        values = {"payload": job.model_dump(mode="json") | {"description": None, "requirements": [], "responsibilities": []},
                                  "status": job.status, "crawler_updated_at": job.updated_at.isoformat(), "content_hash": "", "fetched_at": now}
                        await s.execute(self.db.insert(JobCache).values(job_id=job.id, **values).on_conflict_do_nothing(index_elements=["job_id"]))
                        await s.flush()
                        queued += await self._enqueue(s, user, fresh, job.id, m.score, "new", now)
        async with self.db.session() as s:
            cur = await s.get(CrawlerSyncCursor, "changes")
            cur.resync_since = None
        log.info("resync_done", queued=queued)
        return queued

    # --- housekeeping ------------------------------------------------------------------------------------------
    async def purge(self) -> int:
        """Drop inbox rows older than the retention that every subscription has already passed."""
        async with self.db.session() as s:
            min_wm = (await s.execute(select(func.min(Subscription.watermark_event_id)).where(Subscription.enabled.is_(True)))).scalar()
            limit_id = min_wm if min_wm is not None else await self.global_watermark()
            res = await s.execute(delete(CrawlerEventInbox).where(
                CrawlerEventInbox.status != "pending",
                CrawlerEventInbox.event_id <= limit_id,
                CrawlerEventInbox.ingested_at < utcnow() - timedelta(days=self.retention_days),
            ))
            return res.rowcount or 0

    async def run_once(self) -> int:
        await self.prepare()
        queued = await self.evaluate()
        queued += await self.resync_if_needed()
        return queued

