"""Search sessions (preview / manual search) and subscriptions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..crawler.models import JobOut
from ..db.models import NotificationOutbox, SearchSession, Subscription, SubscriptionMatch
from ..db.session import Database, utcnow
from ..domain.criteria import SearchCriteria
from ..domain.matching import SCORER_VERSION
from ..feed.ingest import FeedIngestor
from ..feed.processor import FeedProcessor
from .search import SearchOutcome, SearchService


class SubscriptionService:
    def __init__(self, db: Database, search: SearchService, ingestor: FeedIngestor, processor: FeedProcessor, *,
                 catchup_budget_s: float = 8.0, keep_results: int = 60) -> None:
        self.db = db
        self.search = search
        self.ingestor = ingestor
        self.processor = processor
        self.catchup_budget_s = catchup_budget_s
        self.keep_results = keep_results

    # --- search sessions ------------------------------------------------------------------------------------
    async def run_search(self, user_id: int, criteria: SearchCriteria, *, source: str = "manual",
                         multipliers: dict | None = None) -> tuple[SearchSession, SearchOutcome]:
        # 1) baseline BEFORE the search: every change after it will be evaluated for a subscription made from this
        baseline = await self.ingestor.catch_up(self.catchup_budget_s)
        # 2) search the current database
        outcome = await self.search.search(criteria, user_id=user_id, multipliers=multipliers)
        results = [
            {"job_id": r.job.id, "score": r.match.score, "match": r.match.to_json(), "job": r.job.model_dump(mode="json")}
            for r in outcome.ranked[: self.keep_results]
        ]
        async with self.db.session() as s:
            sess = SearchSession(user_id=user_id, criteria=criteria.model_dump(mode="json"), results=results, total=len(outcome.ranked),
                                 shown=0, baseline_event_id=baseline, source=source, created_at=utcnow())
            s.add(sess)
            await s.flush()
            return sess, outcome

    async def get_session(self, user_id: int, session_id: int) -> SearchSession | None:
        async with self.db.session() as s:
            sess = await s.get(SearchSession, session_id)
            return sess if sess and sess.user_id == user_id else None

    async def take_page(self, user_id: int, session_id: int, n: int, threshold: int | None = None) -> tuple[list[dict], bool]:
        """Next ``n`` results of a session (above ``threshold`` first); returns (items, has_more)."""
        async with self.db.session() as s:
            sess = await s.get(SearchSession, session_id)
            if sess is None or sess.user_id != user_id:
                return [], False
            items = sess.results
            if threshold:
                above = [r for r in items if r["score"] >= threshold]
                items = above or items  # nothing above: show the best ones, the caller says so
            page = items[sess.shown: sess.shown + n]
            sess.shown += len(page)
            return page, sess.shown < len(items)

    @staticmethod
    def job_from(result: dict[str, Any]) -> JobOut:
        return JobOut.model_validate(result["job"])

    # --- subscriptions ------------------------------------------------------------------------------------------
    async def create_from_session(self, user_id: int, session_id: int, *, name: str, mode: str, profile_version: int | None = None,
                                  notify_updates: bool = False, min_score: int | None = None) -> Subscription | None:
        async with self.db.session() as s:
            sess = await s.get(SearchSession, session_id)
            if sess is None or sess.user_id != user_id:
                return None
            criteria = dict(sess.criteria)
            if min_score is not None:
                criteria["min_score"] = min_score
            sub = Subscription(
                user_id=user_id, name=name[:120], criteria=criteria, version=1, profile_version=profile_version,
                notification_mode=mode, notify_updates=notify_updates, enabled=True,
                start_after_event_id=sess.baseline_event_id, watermark_event_id=sess.baseline_event_id, weights={},
                created_at=utcnow(), updated_at=utcnow(),
            )
            s.add(sub)
            await s.flush()
            # the initial results are "seen": they will never be sent as new vacancies
            for r in sess.results:
                m = r["match"]
                s.add(SubscriptionMatch(subscription_id=sub.id, job_id=r["job_id"], job_revision=r["job"]["updated_at"], score=m["score"],
                                        passed=m["passed"], data=m, origin="initial", notified=False, model_version=SCORER_VERSION,
                                        evaluated_at=utcnow()))
            await s.flush()
            return sub

    async def list(self, user_id: int) -> list[Subscription]:
        async with self.db.session() as s:
            return list((await s.execute(select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id))).scalars())

    async def get(self, user_id: int, sub_id: int) -> Subscription | None:
        async with self.db.session() as s:
            sub = await s.get(Subscription, sub_id)
            return sub if sub and sub.user_id == user_id else None

    async def set_enabled(self, user_id: int, sub_id: int, enabled: bool) -> bool:
        wm = await self.processor.global_watermark() if enabled else 0
        async with self.db.session() as s:
            sub = await s.get(Subscription, sub_id)
            if not sub or sub.user_id != user_id:
                return False
            if enabled and not sub.enabled:
                sub.watermark_event_id = max(sub.watermark_event_id, wm)  # nothing from the pause is sent later
            sub.enabled = enabled
            if not enabled:
                await self._detach_pending(s, user_id, sub_id)
            return True

    async def _detach_pending(self, s, user_id: int, sub_id: int) -> None:
        rows = (await s.execute(select(NotificationOutbox).where(
            NotificationOutbox.user_id == user_id, NotificationOutbox.status == "pending"))).scalars()
        for row in rows:
            if sub_id not in row.subscription_ids:
                continue
            rest = [x for x in row.subscription_ids if x != sub_id]
            if rest:
                row.subscription_ids = rest
            else:
                row.status, row.last_error = "cancelled", "subscription paused or deleted"

    async def delete(self, user_id: int, sub_id: int) -> bool:
        async with self.db.session() as s:
            sub = await s.get(Subscription, sub_id)
            if not sub or sub.user_id != user_id:
                return False
            await self._detach_pending(s, user_id, sub_id)
            await s.delete(sub)
            return True

    async def update(self, user_id: int, sub_id: int, **fields) -> Subscription | None:
        allowed = {"notification_mode", "notify_updates", "name"}
        async with self.db.session() as s:
            sub = await s.get(Subscription, sub_id)
            if not sub or sub.user_id != user_id:
                return None
            for k, v in fields.items():
                if k in allowed:
                    setattr(sub, k, v)
            if "min_score" in fields:
                c = dict(sub.criteria)
                c["min_score"] = int(fields["min_score"])
                sub.criteria = c
                sub.version += 1
            sub.updated_at = utcnow()
            return sub

    async def replace_criteria(self, user_id: int, sub_id: int, criteria: SearchCriteria) -> Subscription | None:
        """New criteria → new version. Already stored matches stay (no archive is re-sent as "new")."""
        async with self.db.session() as s:
            sub = await s.get(Subscription, sub_id)
            if not sub or sub.user_id != user_id:
                return None
            sub.criteria = criteria.model_dump(mode="json")
            sub.version += 1
            sub.updated_at = utcnow()
            return sub

    async def pending_matches(self, user_id: int, limit: int = 10) -> list[SubscriptionMatch]:
        """Feed matches that were not pushed (MANUAL mode) — shown on request."""
        async with self.db.session() as s:
            rows = await s.execute(
                select(SubscriptionMatch).join(Subscription, Subscription.id == SubscriptionMatch.subscription_id)
                .where(Subscription.user_id == user_id, SubscriptionMatch.origin != "initial", SubscriptionMatch.passed.is_(True),
                       SubscriptionMatch.notified.is_(False))
                .order_by(SubscriptionMatch.evaluated_at.desc()).limit(limit)
            )
            return list(rows.scalars())
