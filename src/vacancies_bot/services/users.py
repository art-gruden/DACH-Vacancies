"""Users, profiles and questionnaire drafts."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select

from ..db.models import BotUser, CandidateProfileRow, QuestionnaireDraft, SearchSession
from ..db.session import Database, utcnow
from ..domain.criteria import CandidateProfile


class UserService:
    def __init__(self, db: Database, default_language: str = "ru") -> None:
        self.db = db
        self.default_language = default_language

    async def ensure(self, user_id: int, chat_id: int, tg_lang: str | None = None) -> BotUser:
        async with self.db.session() as s:
            u = await s.get(BotUser, user_id)
            if u is None:
                lang = (tg_lang or "")[:2]
                u = BotUser(id=user_id, chat_id=chat_id, ui_language=lang if lang in ("ru", "uk", "de", "en") else self.default_language)
                s.add(u)
            else:
                if u.chat_id != chat_id:
                    u.chat_id = chat_id
                if u.blocked_at is not None:
                    u.blocked_at = None  # the user wrote again → unblocked
            await s.flush()
            return u

    async def get(self, user_id: int) -> BotUser | None:
        async with self.db.session() as s:
            return await s.get(BotUser, user_id)

    async def set_language(self, user_id: int, lang: str) -> None:
        async with self.db.session() as s:
            u = await s.get(BotUser, user_id)
            if u:
                u.ui_language = lang

    async def set_ai_consent(self, user_id: int, version: int | None) -> None:
        async with self.db.session() as s:
            u = await s.get(BotUser, user_id)
            if u:
                u.ai_consent_version = version
                u.ai_consent_at = utcnow() if version else None

    async def set_cv_consent(self, user_id: int, given: bool) -> None:
        async with self.db.session() as s:
            u = await s.get(BotUser, user_id)
            if u:
                u.cv_consent_at = utcnow() if given else None

    # --- profile ---------------------------------------------------------------------------------------
    async def load_profile(self, user_id: int) -> CandidateProfile | None:
        async with self.db.session() as s:
            row = await s.get(CandidateProfileRow, user_id)
            return CandidateProfile.model_validate(row.data) if row else None

    async def save_profile(self, user_id: int, profile: CandidateProfile) -> int:
        async with self.db.session() as s:
            row = await s.get(CandidateProfileRow, user_id)
            version = (row.version + 1) if row else 1
            profile.profile_version = version
            profile.confirmed_at = utcnow()
            data = profile.model_dump(mode="json")
            if row:
                row.version, row.data, row.confirmed_at = version, data, profile.confirmed_at
            else:
                s.add(CandidateProfileRow(user_id=user_id, version=version, data=data, confirmed_at=profile.confirmed_at))
            u = await s.get(BotUser, user_id)
            if u:
                n = profile.notifications
                u.ui_language = profile.ui_language
                u.timezone, u.quiet_start, u.quiet_end, u.daily_limit = n.timezone, n.quiet_start, n.quiet_end, n.daily_limit
            return version

    # --- questionnaire drafts ------------------------------------------------------------------------------
    async def load_draft(self, user_id: int) -> dict | None:
        async with self.db.session() as s:
            row = await s.get(QuestionnaireDraft, user_id)
            return dict(row.data) if row else None

    async def save_draft(self, user_id: int, draft: dict) -> None:
        async with self.db.session() as s:
            stmt = self.db.insert(QuestionnaireDraft).values(user_id=user_id, data=draft, updated_at=utcnow())
            await s.execute(stmt.on_conflict_do_update(index_elements=["user_id"], set_={"data": draft, "updated_at": utcnow()}))

    async def drop_draft(self, user_id: int) -> None:
        async with self.db.session() as s:
            await s.execute(delete(QuestionnaireDraft).where(QuestionnaireDraft.user_id == user_id))

    # --- deletion ----------------------------------------------------------------------------------------------
    async def delete_everything(self, user_id: int) -> None:
        """Real deletion: the user row and, by ON DELETE CASCADE, profile, drafts, subscriptions, matches, saved
        jobs, feedback, outbox, search sessions and CV records. ``ai_usage`` keeps anonymous cost rows."""
        async with self.db.session() as s:
            u = await s.get(BotUser, user_id)
            if u:
                await s.delete(u)

    async def purge_old_sessions(self, days: int = 2) -> int:
        async with self.db.session() as s:
            res = await s.execute(delete(SearchSession).where(SearchSession.created_at < utcnow() - timedelta(days=days)))
            return res.rowcount or 0

    async def active_user_ids(self) -> list[int]:
        async with self.db.session() as s:
            return list((await s.execute(select(BotUser.id).where(BotUser.blocked_at.is_(None)))).scalars())
