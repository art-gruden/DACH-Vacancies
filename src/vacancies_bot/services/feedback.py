"""Feedback buttons → soft ranking weights. Hard constraints are never changed automatically."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from ..db.models import Feedback, SavedJob, Subscription
from ..db.session import Database, utcnow

REASON_COMPONENT = {"role": "role", "language": "language", "salary": "other", "geo": "geo", "level": "experience"}
STEP = 1.15
SUGGEST_AFTER = 3


class FeedbackService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(self, user_id: int, job_id: int, response: str, *, sub_id: int | None = None, reason: str | None = None) -> str | None:
        """Stores the signal; for a dislike with a reason, raises that component's weight in the subscription.
        Returns the reason when the same reason was given often enough to *suggest* changing the criteria."""
        async with self.db.session() as s:
            s.add(Feedback(user_id=user_id, subscription_id=sub_id, job_id=job_id, response=response, reason=reason, created_at=utcnow()))
            if not (response == "dislike" and reason and sub_id):
                return None
            sub = await s.get(Subscription, sub_id)
            if sub is None or sub.user_id != user_id:
                return None
            comp = REASON_COMPONENT.get(reason)
            if comp:
                w = dict(sub.weights or {})
                w[comp] = round(min(2.0, w.get(comp, 1.0) * STEP), 3)
                sub.weights = w
            await s.flush()
            n = (await s.execute(select(func.count()).select_from(Feedback).where(
                Feedback.subscription_id == sub_id, Feedback.reason == reason, Feedback.response == "dislike",
                Feedback.created_at > utcnow() - timedelta(days=30)))).scalar()
            return reason if n and n % SUGGEST_AFTER == 0 else None

    async def save_job(self, user_id: int, job_id: int, title: str, company: str, url: str, status: str = "saved") -> None:
        async with self.db.session() as s:
            row = await s.get(SavedJob, (user_id, job_id))
            if row:
                row.status = status if status != "saved" else row.status
            else:
                s.add(SavedJob(user_id=user_id, job_id=job_id, title=title[:300], company=company[:200], url=url, status=status))

    async def saved(self, user_id: int) -> list[SavedJob]:
        async with self.db.session() as s:
            return list((await s.execute(select(SavedJob).where(SavedJob.user_id == user_id).order_by(SavedJob.created_at.desc()).limit(30))).scalars())

    async def set_status(self, user_id: int, job_id: int, status: str) -> bool:
        async with self.db.session() as s:
            row = await s.get(SavedJob, (user_id, job_id))
            if not row:
                return False
            row.status = status
            return True
