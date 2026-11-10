"""Telegram Bot Service storage. Separate database from the crawler: no user data ever goes to the crawler.

Deleting a ``bot_users`` row removes every personal record (ON DELETE CASCADE); ``ai_usage`` keeps only
anonymous cost rows (user id set to NULL).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _now_col(**kw):
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kw)


class BotUser(Base):
    __tablename__ = "bot_users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user id
    chat_id: Mapped[int] = mapped_column(BigInteger)
    ui_language: Mapped[str] = mapped_column(String(5), default="ru")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Berlin")
    quiet_start: Mapped[str | None] = mapped_column(String(5), default="22:00")
    quiet_end: Mapped[str | None] = mapped_column(String(5), default="08:00")
    daily_limit: Mapped[int] = mapped_column(Integer, default=5)
    ai_consent_version: Mapped[int | None] = mapped_column(Integer)
    ai_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cv_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # user blocked the bot
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())


class CandidateProfileRow(Base):
    __tablename__ = "candidate_profiles"
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JsonType)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())


class QuestionnaireDraft(Base):
    """Restart-safe questionnaire state (aiogram's in-memory FSM would lose it)."""

    __tablename__ = "questionnaire_drafts"
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    criteria: Mapped[dict] = mapped_column(JsonType)
    version: Mapped[int] = mapped_column(Integer, default=1)
    profile_version: Mapped[int | None] = mapped_column(Integer)
    notification_mode: Mapped[str] = mapped_column(String(16), default="IMMEDIATE")
    notify_updates: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # change-feed position: events with id > watermark have not been evaluated for this subscription yet
    start_after_event_id: Mapped[int] = mapped_column(BigInteger, default=0)
    watermark_event_id: Mapped[int] = mapped_column(BigInteger, default=0)
    weights: Mapped[dict] = mapped_column(JsonType, default=dict)  # soft weight multipliers from feedback
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())


class SubscriptionMatch(Base):
    __tablename__ = "subscription_matches"
    __table_args__ = (UniqueConstraint("subscription_id", "job_id", name="uq_match_sub_job"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(BigInteger, index=True)
    job_revision: Mapped[str | None] = mapped_column(String(40))  # crawler updated_at
    score: Mapped[int] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean)
    data: Mapped[dict] = mapped_column(JsonType)  # reasons, gaps, unknowns, completeness
    origin: Mapped[str] = mapped_column(String(16))  # initial | feed | reopened
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_explanation: Mapped[dict | None] = mapped_column(JsonType)
    model_version: Mapped[str] = mapped_column(String(40))
    evaluated_at: Mapped[datetime] = _now_col()


class SavedJob(Base):
    __tablename__ = "saved_jobs"
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), primary_key=True)
    job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="saved")  # saved|applied|interview|rejected|hired
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    job_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id", ondelete="SET NULL"))
    job_id: Mapped[int] = mapped_column(BigInteger)
    response: Mapped[str] = mapped_column(String(16))  # like|dislike|save|applied
    reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = _now_col()


class CrawlerEventInbox(Base):
    __tablename__ = "crawler_event_inbox"
    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    event_type: Mapped[str] = mapped_column(String(32))
    job_id: Mapped[int] = mapped_column(BigInteger, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fields: Mapped[list | None] = mapped_column(JsonType)
    reason: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | ready | skipped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = _now_col()
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_inbox_status_event", "status", "event_id"),)


class CrawlerSyncCursor(Base):
    __tablename__ = "crawler_sync_cursor"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    cursor: Mapped[str] = mapped_column(Text)
    last_event_id: Mapped[int] = mapped_column(BigInteger, default=0)
    resyncs: Mapped[int] = mapped_column(Integer, default=0)
    resync_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set after 400/409: re-sync via /jobs
    last_poll_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())


class JobCache(Base):
    """Last known public vacancy detail (crawler data, not personal data)."""

    __tablename__ = "job_cache"
    job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    payload: Mapped[dict] = mapped_column(JsonType)
    status: Mapped[str] = mapped_column(String(12))
    crawler_updated_at: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = _now_col()


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))  # new | update | reopened
    dedup_key: Mapped[str] = mapped_column(String(120), unique=True)
    subscription_ids: Mapped[list] = mapped_column(JsonType, default=list)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|sending|sent|failed|cancelled|unknown|deferred
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(300))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    batch_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = _now_col()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_outbox_status_due", "status", "due_at"),)


class SearchSession(Base):
    """Ranked results of a manual/initial search for paging ("more") and buttons. Short-lived."""

    __tablename__ = "search_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), index=True)
    criteria: Mapped[dict] = mapped_column(JsonType)
    results: Mapped[list] = mapped_column(JsonType)  # [{"job_id", "score", "data"}]
    total: Mapped[int] = mapped_column(Integer, default=0)
    shown: Mapped[int] = mapped_column(Integer, default=0)
    baseline_event_id: Mapped[int] = mapped_column(BigInteger, default=0)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # profile|manual|assistant
    created_at: Mapped[datetime] = _now_col()


class CvDocument(Base):
    __tablename__ = "cv_documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), index=True)
    file_sha256: Mapped[str] = mapped_column(String(64))
    mime: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="received")  # received|extracted|confirmed|deleted|failed
    encrypted_text: Mapped[bytes | None] = mapped_column(LargeBinary)  # scrubbed text, Fernet; never the file
    extracted: Mapped[dict | None] = mapped_column(JsonType)  # unconfirmed facts awaiting review
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id", ondelete="SET NULL"), index=True)
    purpose: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(80))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error_kind: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = _now_col()


class EmbeddingCache(Base):
    __tablename__ = "embedding_cache"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)  # job:<id>:<hash> | profile:<user>:<version>
    model: Mapped[str] = mapped_column(String(80))
    vector: Mapped[list] = mapped_column(JsonType)
    created_at: Mapped[datetime] = _now_col()


class FacetCache(Base):
    __tablename__ = "facet_cache"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    payload: Mapped[list] = mapped_column(JsonType)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
