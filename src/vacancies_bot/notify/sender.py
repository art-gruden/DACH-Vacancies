"""Delivers the notification outbox to Telegram.

Delivery is *at most once* for ambiguous failures: a row is leased (``sending``) and committed before the
Telegram call; if the process dies during the call the row becomes ``unknown`` after the lease and is not
resent (a lost message is better than a burst of duplicates after every restart). Definite failures (network
error before sending, 429, 5xx) are retried with backoff. A blocked bot disables the user's subscriptions.

Limits: quiet hours and the daily limit are checked at send time in the user's time zone; when several items
are due at once (``digest_threshold``) or come from a digest-mode subscription, they go out as one message.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import distinct, func, select, update

from ..bot.keyboards import job_buttons
from ..bot.render import job_card, reason_text
from ..bot.texts import esc, t
from ..crawler.models import JobDetail
from ..db.models import BotUser, JobCache, NotificationOutbox, Subscription, SubscriptionMatch
from ..db.session import Database, utcnow
from ..logging import get_logger
from ..questionnaire.engine import Button
from .planner import in_quiet_hours, local_day_start, next_local_hour

log = get_logger(__name__)


class MessengerBlocked(Exception):
    """The user blocked the bot or the chat no longer exists."""


class MessengerRetryAfter(Exception):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"retry after {seconds}s")
        self.seconds = seconds


class MessengerTransient(Exception):
    """Definitely not delivered (connection refused, 5xx before acceptance) → safe to retry."""


class Messenger(Protocol):
    async def send(self, chat_id: int, text: str, rows: list[list[Button]] | None = None) -> int: ...


@dataclass
class SendStats:
    sent_messages: int = 0
    items: int = 0
    deferred: int = 0
    cancelled: int = 0
    failed: int = 0


class Sender:
    def __init__(self, db: Database, messenger: Messenger, *, digest_threshold: int = 3, lease_s: int = 120,
                 morning_hour: int = 8, max_attempts: int = 5) -> None:
        self.db = db
        self.messenger = messenger
        self.digest_threshold = digest_threshold
        self.lease_s = lease_s
        self.morning_hour = morning_hour
        self.max_attempts = max_attempts

    async def recover_leases(self, now: datetime) -> int:
        async with self.db.session() as s:
            res = await s.execute(
                update(NotificationOutbox)
                .where(NotificationOutbox.status == "sending", NotificationOutbox.lease_until < now)
                .values(status="unknown", last_error="lease expired during send; not retried to avoid duplicates")
            )
            n = res.rowcount or 0
        if n:
            log.warning("outbox_ambiguous_sends", rows=n)
        return n

    async def run_once(self, now: datetime | None = None) -> SendStats:
        now = now or utcnow()
        stats = SendStats()
        await self.recover_leases(now)
        async with self.db.session() as s:
            users = list((await s.execute(
                select(NotificationOutbox.user_id).where(NotificationOutbox.status == "pending", NotificationOutbox.due_at <= now).distinct().limit(100)
            )).scalars())
        for uid in users:
            await self._send_user(uid, now, stats)
        return stats

    async def _sent_today(self, s, user: BotUser, now: datetime) -> int:
        start = local_day_start(now, user.timezone)
        q = select(func.count(distinct(NotificationOutbox.batch_id))).where(
            NotificationOutbox.user_id == user.id, NotificationOutbox.status.in_(("sent", "unknown")), NotificationOutbox.sent_at >= start)
        return int((await s.execute(q)).scalar() or 0)

    async def _send_user(self, uid: int, now: datetime, stats: SendStats) -> None:
        async with self.db.session() as s:
            user = await s.get(BotUser, uid)
            rows = list((await s.execute(
                select(NotificationOutbox).where(NotificationOutbox.user_id == uid, NotificationOutbox.status == "pending",
                                                 NotificationOutbox.due_at <= now).order_by(NotificationOutbox.id)
            )).scalars())
            if not rows or user is None:
                return
            if user.blocked_at is not None:
                for r in rows:
                    r.status = "cancelled"
                stats.cancelled += len(rows)
                return
            quiet_end = in_quiet_hours(now, user.timezone, user.quiet_start, user.quiet_end)
            if quiet_end:
                for r in rows:
                    r.due_at = quiet_end
                stats.deferred += len(rows)
                return
            jobs = {c.job_id: c for c in (await s.execute(select(JobCache).where(JobCache.job_id.in_({r.job_id for r in rows})))).scalars()}
            live = []
            for r in rows:
                cache = jobs.get(r.job_id)
                if cache is None or cache.status != "open":
                    r.status, r.last_error = "cancelled", "job closed or unknown"
                    stats.cancelled += 1
                else:
                    live.append(r)
            if not live:
                return
            budget = max(0, user.daily_limit - await self._sent_today(s, user, now))
            if budget == 0:
                tomorrow = next_local_hour(now, user.timezone, self.morning_hour)
                for r in live:
                    r.due_at = tomorrow
                    r.payload = {**r.payload, "digest": True}
                stats.deferred += len(live)
                return
            subs = {x.id: x for x in (await s.execute(select(Subscription).where(Subscription.user_id == uid))).scalars()}
            live.sort(key=lambda r: max(r.payload.get("scores", {"0": 0}).values()), reverse=True)
            digest = len(live) >= self.digest_threshold or any(r.payload.get("digest") for r in live) or budget < len(live)
            batches = [live[:10]] if digest else [[r] for r in live[:budget]]
            plan = []
            lease = now + timedelta(seconds=self.lease_s)
            for batch in batches:
                bid = uuid.uuid4().hex
                for r in batch:
                    r.status, r.lease_until, r.batch_id, r.attempts = "sending", lease, bid, r.attempts + 1
                plan.append((bid, [r.id for r in batch]))
            payload = {
                "lang": user.ui_language, "chat_id": user.chat_id,
                "rows": {r.id: (r.kind, r.job_id, r.payload, list(r.subscription_ids)) for r in live},
                "jobs": {jid: c.payload for jid, c in jobs.items()},
                "subs": {sid: sub.name for sid, sub in subs.items()},
                "digest": digest,
            }
            matches = {}
            for r in live:
                best = max(r.subscription_ids, key=lambda sid: r.payload.get("scores", {}).get(str(sid), 0))
                m = (await s.execute(select(SubscriptionMatch.data).where(
                    SubscriptionMatch.subscription_id == best, SubscriptionMatch.job_id == r.job_id))).scalar()
                matches[r.id] = (best, m or {"score": r.payload.get("scores", {}).get(str(best), 0)})
            payload["matches"] = matches
        # --- outside the transaction: talk to Telegram, one batch at a time ---------------------------------------
        for _bid, ids in plan:
            text, buttons = self._render(payload, ids)
            try:
                msg_id = await self.messenger.send(payload["chat_id"], text, buttons)
            except MessengerBlocked:
                await self._blocked(uid)
                stats.cancelled += len(ids)
                return
            except MessengerRetryAfter as exc:
                await self._retry(ids, now + timedelta(seconds=exc.seconds + 1), str(exc))
                stats.deferred += len(ids)
                return
            except MessengerTransient as exc:
                await self._retry(ids, now + timedelta(seconds=60), str(exc))
                stats.failed += len(ids)
                continue
            except Exception as exc:  # unknown outcome → do not resend
                async with self.db.session() as s:
                    await s.execute(update(NotificationOutbox).where(NotificationOutbox.id.in_(ids))
                                    .values(status="unknown", last_error=f"{type(exc).__name__}"[:300], sent_at=now))
                log.error("send_failed_unknown", user=uid, error=type(exc).__name__)
                stats.failed += len(ids)
                continue
            async with self.db.session() as s:
                await s.execute(update(NotificationOutbox).where(NotificationOutbox.id.in_(ids))
                                .values(status="sent", telegram_message_id=msg_id, sent_at=now, lease_until=None))
            stats.sent_messages += 1
            stats.items += len(ids)

    async def _retry(self, ids: list[int], when: datetime, err: str) -> None:
        async with self.db.session() as s:
            rows = (await s.execute(select(NotificationOutbox).where(NotificationOutbox.id.in_(ids)))).scalars()
            for r in rows:
                r.lease_until = None
                r.last_error = err[:300]
                if r.attempts >= self.max_attempts:
                    r.status = "failed"
                else:
                    r.status, r.due_at = "pending", when

    async def _blocked(self, uid: int) -> None:
        async with self.db.session() as s:
            user = await s.get(BotUser, uid)
            if user:
                user.blocked_at = utcnow()
            await s.execute(update(Subscription).where(Subscription.user_id == uid).values(enabled=False))
            await s.execute(update(NotificationOutbox).where(NotificationOutbox.user_id == uid,
                                                             NotificationOutbox.status.in_(("pending", "sending")))
                            .values(status="cancelled", last_error="bot blocked by user"))
        log.info("user_blocked_bot", user=uid)

    def _render(self, p: dict, ids: list[int]) -> tuple[str, list[list[Button]] | None]:
        lang = p["lang"]
        if p["digest"] or len(ids) > 1:
            lines = [t(lang, "digest.title", n=len(ids)), ""]
            for i, rid in enumerate(ids, 1):
                kind, job_id, payload, sub_ids = p["rows"][rid]
                job = JobDetail.model_validate(p["jobs"][job_id])
                _, match = p["matches"][rid]
                score = match.get("score", 0)
                where = job.locations[0].city if job.locations and job.locations[0].city else (job.work_model.title())
                lines.append(f"{i}. <b>{score}</b> · <a href=\"{esc(job.url)}\">{esc(job.title)}</a> — {esc(job.company.name)} · {esc(where)}")
                why = match.get("why") or []
                if why:
                    lines.append("   " + "; ".join(reason_text(r, lang) for r in why[:2]))
            lines.append("")
            lines.append(f"<i>{t(lang, 'card.disclaimer')}</i>")
            buttons = [[Button(f"{i}. {t(lang, 'card.why_btn', score=p['matches'][rid][1].get('score', 0))}",
                               f"j:w:{p['rows'][rid][1]}:s{p['matches'][rid][0]}")] for i, rid in enumerate(ids, 1)]
            return "\n".join(lines), buttons[:10]
        rid = ids[0]
        kind, job_id, payload, sub_ids = p["rows"][rid]
        job = JobDetail.model_validate(p["jobs"][job_id])
        best, match = p["matches"][rid]
        header = None
        if kind == "update":
            header = t(lang, "card.update", fields=", ".join(payload.get("fields") or []))
        elif kind == "reopened":
            header = t(lang, "card.reopened")
        names = [p["subs"].get(sid, str(sid)) for sid in sub_ids]
        text = job_card(job, match, lang, header=header, sub_names=names if len(names) > 1 else None)
        return text, job_buttons(lang, job.id, job.url, match.get("score", 0), ctx=f"s{best}")


async def outbox_counts(db: Database) -> dict[str, int]:
    async with db.session() as s:
        rows = await s.execute(select(NotificationOutbox.status, func.count()).group_by(NotificationOutbox.status))
        return {k: v for k, v in rows.all()}

