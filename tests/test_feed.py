from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from conftest import devops_criteria
from vacancies_bot.db.models import (
    BotUser,
    CrawlerEventInbox,
    CrawlerSyncCursor,
    NotificationOutbox,
    SavedJob,
    Subscription,
    SubscriptionMatch,
)
from vacancies_bot.db.session import utcnow
from vacancies_bot.feed.ingest import FeedIngestor
from vacancies_bot.feed.processor import FeedProcessor
from vacancies_bot.notify.planner import due_at, in_quiet_hours
from vacancies_bot.notify.sender import MessengerBlocked, MessengerRetryAfter, Sender
from vacancies_bot.services.search import SearchService
from vacancies_bot.services.subscriptions import SubscriptionService
from vacancies_bot.services.users import UserService
from vacancies_bot.testing.fake_crawler import NOW, L, make_job

UID = 1001


class FakeMessenger:
    def __init__(self):
        self.sent: list[tuple[int, str, list]] = []
        self.fail: Exception | None = None

    async def send(self, chat_id, text, rows=None):
        if self.fail:
            raise self.fail
        self.sent.append((chat_id, text, rows))
        return len(self.sent)


class Env:
    def __init__(self, db, client, fake):
        self.db, self.client, self.fake = db, client, fake
        self.users = UserService(db)
        self.ingestor = FeedIngestor(db, client, page_size=2)  # small pages exercise paging
        self.processor = FeedProcessor(db, client, max_attempts=3)
        self.subs = SubscriptionService(db, SearchService(client), self.ingestor, self.processor, catchup_budget_s=2)
        self.messenger = FakeMessenger()
        self.sender = Sender(db, self.messenger, digest_threshold=3)

    async def user(self, uid=UID, **kw):
        await self.users.ensure(uid, uid * 10, "ru")
        async with self.db.session() as s:
            u = await s.get(BotUser, uid)
            u.quiet_start, u.quiet_end = None, None  # deterministic tests; quiet hours are tested separately
            for k, v in kw.items():
                setattr(u, k, v)

    async def subscribe(self, criteria=None, uid=UID, mode="IMMEDIATE", name="DevOps", **kw):
        sess, _ = await self.subs.run_search(uid, criteria or devops_criteria())
        return await self.subs.create_from_session(uid, sess.id, name=name, mode=mode, **kw), sess

    async def cycle(self, now=None):
        await self.ingestor.poll()
        await self.processor.run_once()
        return await self.sender.run_once(now or utcnow() + timedelta(seconds=1))

    async def outbox(self):
        async with self.db.session() as s:
            return list((await s.execute(select(NotificationOutbox).order_by(NotificationOutbox.id))).scalars())


def new_sre(job_id=20, **kw):
    base = dict(company="Neue Firma GmbH", city=1, work_model="REMOTE", remote_scope="countries", remote_countries=["DE"],
                employment=["FULL_TIME"], seniority="SENIOR", skills=["Kubernetes", "Terraform"], languages=[L("en", "B2")], age_days=0)
    base.update(kw)
    return make_job(job_id, kw.pop("title", "SRE Kubernetes (m/w/d)"), **{k: v for k, v in base.items() if k != "title"})


@pytest.fixture
async def env(db, client, fake):
    e = Env(db, client, fake)
    await e.user()
    return e


async def test_initial_results_not_resent_and_gap_events_not_lost(env):
    fake = env.fake
    fake.emit("job.created", 7)  # history before the bot started: never replayed
    await env.ingestor.poll()  # cursor initialised from /changes/head
    sess, outcome = await env.subs.run_search(UID, devops_criteria())
    assert {r.job.id for r in outcome.ranked} == {1, 2, 6, 9, 10, 14}
    # between the preview and the click on "Subscribe": a new vacancy and an update of an initial result
    fake.add_job(new_sre(20))
    fake.update_job(1, title="Senior Site Reliability Engineer (m/w/d)")
    sub = await env.subs.create_from_session(UID, sess.id, name="DevOps", mode="IMMEDIATE")
    stats = await env.cycle()
    assert stats.sent_messages == 1 and stats.items == 1
    assert "SRE Kubernetes" in env.messenger.sent[0][1]
    # nothing is sent twice: more cycles, a replay of the same feed page, a processor re-run
    await env.cycle()
    async with env.db.session() as s:  # re-evaluate the same events again (e.g. crash before the watermark moved)
        await s.execute(update(Subscription).where(Subscription.id == sub.id).values(watermark_event_id=sub.start_after_event_id))
    stats = await env.cycle()
    assert stats.sent_messages == 0 and len(env.messenger.sent) == 1
    async with env.db.session() as s:
        origins = dict((await s.execute(select(SubscriptionMatch.job_id, SubscriptionMatch.origin)
                                        .where(SubscriptionMatch.subscription_id == sub.id))).all())
    assert origins[20] == "feed" and origins[1] == "initial" and 7 not in origins


async def test_crash_between_page_and_cursor_is_idempotent(env):
    await env.ingestor.poll()
    await env.subscribe()
    for jid in (20, 21, 22):
        env.fake.add_job(new_sre(jid, company=f"Firma {jid}"))
    await env.ingestor.poll()
    # simulate a lost cursor commit: move the cursor back to "after event 1" → events 2..3 are read again
    head0 = await env.client.changes(None, limit=1)
    async with env.db.session() as s:
        cur = await s.get(CrawlerSyncCursor, "changes")
        cur.cursor = head0.next_cursor
    assert await env.ingestor.poll() == 0  # re-read pages, nothing new stored
    stats = await env.cycle()
    assert stats.items == 3 and stats.sent_messages == 1  # 3 items due at once → one digest
    assert "Новые вакансии: 3" in env.messenger.sent[0][1]


async def test_same_job_in_two_subscriptions_is_one_message(env):
    await env.ingestor.poll()
    await env.subscribe(name="DevOps DE/CH")
    await env.subscribe(devops_criteria(search_breadth="BROAD", min_score=0), name="Wide")
    env.fake.add_job(new_sre(20))
    stats = await env.cycle()
    assert stats.sent_messages == 1
    rows = await env.outbox()
    assert len(rows) == 1 and len(rows[0].subscription_ids) == 2
    assert "DevOps DE/CH" in env.messenger.sent[0][1] and "Wide" in env.messenger.sent[0][1]


async def test_closed_vacancy_cancels_pending_and_marks_saved(env):
    await env.ingestor.poll()
    await env.subscribe(mode="EVENING")
    env.fake.add_job(new_sre(20))
    await env.cycle()
    async with env.db.session() as s:
        s.add(SavedJob(user_id=UID, job_id=20, title="x", company="y", url="u"))
    assert [r.status for r in await env.outbox()] == ["pending"]
    env.fake.close_job(20)
    await env.cycle()
    assert [r.status for r in await env.outbox()] == ["cancelled"]
    async with env.db.session() as s:
        assert (await s.get(SavedJob, (UID, 20))).job_closed
    assert env.messenger.sent == []


async def test_quiet_hours_daily_limit_and_digest(env):
    berlin_23 = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)  # 23:00 in Berlin
    assert in_quiet_hours(berlin_23, "Europe/Berlin", "22:00", "08:00") == datetime(2026, 11, 11, 7, 0, tzinfo=UTC)
    assert in_quiet_hours(berlin_23.replace(hour=12), "Europe/Berlin", "22:00", "08:00") is None
    assert due_at(berlin_23, "MANUAL", tz="Europe/Berlin", quiet_start=None, quiet_end=None) is None
    assert due_at(berlin_23, "MORNING", tz="Europe/Berlin", quiet_start=None, quiet_end=None).astimezone(UTC).hour == 7  # 08:00 Berlin
    await env.user(daily_limit=2)
    await env.ingestor.poll()
    await env.subscribe()
    async with env.db.session() as s:
        u = await s.get(BotUser, UID)
        u.quiet_start, u.quiet_end = "22:00", "08:00"
    env.fake.add_job(new_sre(20))
    await env.ingestor.poll()
    await env.processor.run_once()
    night = utcnow().replace(hour=22, minute=30)  # inside quiet hours in Berlin
    stats = await env.sender.run_once(night if night > utcnow() else night + timedelta(days=1))
    assert stats.deferred == 1 and env.messenger.sent == []
    async with env.db.session() as s:
        u = await s.get(BotUser, UID)
        u.quiet_start = u.quiet_end = None
    # limit 2 messages/day: two single cards, then the rest waits for the morning as a digest
    for jid in (21, 22, 23, 24):
        env.fake.add_job(new_sre(jid, company=f"F{jid}"))
        await env.cycle(utcnow() + timedelta(days=1))  # one by one → single cards
    assert len(env.messenger.sent) == 2
    rows = [r for r in await env.outbox() if r.status == "pending"]
    assert rows and all(r.payload.get("digest") for r in rows)


async def test_ambiguous_send_is_not_repeated(env):
    await env.ingestor.poll()
    await env.subscribe()
    env.fake.add_job(new_sre(20))
    env.messenger.fail = RuntimeError("connection reset after write")
    await env.cycle()
    env.messenger.fail = None
    await env.cycle()
    assert env.messenger.sent == [] and [r.status for r in await env.outbox()] == ["unknown"]
    # a process crash during the call leaves a leased row → it becomes "unknown", not resent
    env.fake.add_job(new_sre(21, company="F21"))
    await env.ingestor.poll()
    await env.processor.run_once()
    async with env.db.session() as s:
        await s.execute(update(NotificationOutbox).where(NotificationOutbox.job_id == 21)
                        .values(status="sending", lease_until=utcnow() - timedelta(seconds=1)))
    await env.sender.run_once(utcnow() + timedelta(seconds=1))
    assert env.messenger.sent == [] and {r.status for r in await env.outbox()} == {"unknown"}


async def test_retry_after_and_blocked_user(env):
    await env.ingestor.poll()
    sub, _ = await env.subscribe()
    env.fake.add_job(new_sre(20))
    env.messenger.fail = MessengerRetryAfter(30)
    await env.cycle()
    row = (await env.outbox())[0]
    assert row.status == "pending" and row.attempts == 1
    env.messenger.fail = MessengerBlocked()
    await env.sender.run_once(utcnow() + timedelta(minutes=5))
    async with env.db.session() as s:
        assert (await s.get(BotUser, UID)).blocked_at is not None
        assert not (await s.get(Subscription, sub.id)).enabled
    assert (await env.outbox())[0].status == "cancelled"


async def test_crawler_outage_blocks_then_recovers_in_order(env):
    await env.ingestor.poll()
    await env.subscribe()
    env.fake.add_job(new_sre(20))
    await env.ingestor.poll()
    env.fake.down = True
    await env.processor.run_once()
    async with env.db.session() as s:
        ev = (await s.execute(select(CrawlerEventInbox))).scalar_one()
        assert ev.status == "pending" and ev.attempts == 1
        ev.next_attempt_at = None
    assert await env.processor.global_watermark() == 0
    env.fake.down = False
    stats = await env.cycle()
    assert stats.items == 1


async def test_cursor_ahead_triggers_resync_via_jobs(env):
    env.fake.emit("job.created", 1)
    await env.ingestor.poll()
    await env.subscribe()
    env.fake.events.clear()  # crawler database restored from an older backup → 409
    env.fake.jobs[30] = new_sre(30, company="Nach Restore GmbH")
    env.fake.jobs[30]["first_seen_at"] = (NOW + timedelta(days=3650)).isoformat()  # surely after the last good poll
    await env.ingestor.poll()
    async with env.db.session() as s:
        cur = await s.get(CrawlerSyncCursor, "changes")
        assert cur.resyncs == 1 and cur.resync_since is not None
    stats = await env.cycle()
    assert stats.items == 1 and "Nach Restore" in env.messenger.sent[0][1]
    async with env.db.session() as s:
        assert (await s.get(CrawlerSyncCursor, "changes")).resync_since is None


async def test_manual_mode_and_update_opt_in(env):
    await env.ingestor.poll()
    manual, _ = await env.subscribe(mode="MANUAL", name="Manual")
    env.fake.add_job(new_sre(20))
    await env.cycle()
    assert env.messenger.sent == [] and await env.outbox() == []
    pending = await env.subs.pending_matches(UID)
    assert [m.job_id for m in pending] == [20]
    await env.subs.delete(UID, manual.id)
    sub, _ = await env.subscribe(name="Updates", notify_updates=True)
    env.fake.add_job(new_sre(21, company="F21"))
    await env.cycle()
    env.fake.update_job(21, salary={"min": 90000, "max": 100000, "currency": "EUR", "period": "year", "basis": "gross", "raw": "90-100k", "confidence": 1})
    env.fake.update_job(21, last_seen_at=NOW.isoformat())  # not meaningful → no message
    await env.cycle()
    kinds = [r.kind for r in await env.outbox()]
    assert kinds == ["new", "update"]
    assert len(env.messenger.sent) == 2 and "обновлена" in env.messenger.sent[1][1]


async def test_pause_and_resume_skips_events_during_pause(env):
    await env.ingestor.poll()
    sub, _ = await env.subscribe()
    await env.subs.set_enabled(UID, sub.id, False)
    env.fake.add_job(new_sre(20))
    await env.cycle()
    await env.subs.set_enabled(UID, sub.id, True)
    await env.cycle()
    assert env.messenger.sent == []
    env.fake.add_job(new_sre(21, company="F21"))
    stats = await env.cycle()
    assert stats.items == 1


async def test_delete_me_removes_everything(env):
    await env.ingestor.poll()
    await env.subscribe()
    env.fake.add_job(new_sre(20))
    await env.cycle()
    await env.users.delete_everything(UID)
    async with env.db.session() as s:
        for model in (Subscription, SubscriptionMatch, NotificationOutbox):
            assert (await s.execute(select(model))).first() is None
