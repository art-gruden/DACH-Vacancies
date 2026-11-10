"""Telegram handlers (aiogram 3). Thin layer: all decisions live in services/engine; this file renders and routes.

Callback prefixes: ``qn`` questionnaire · ``st`` start mode · ``m`` menu · ``ss`` search session · ``j`` job card ·
``sub`` subscriptions · ``sv`` saved · ``pf`` parsed facts · ``ai`` consent · ``cv`` CV · ``del`` deletion · ``lang``.
"""

from __future__ import annotations

import io
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from ..ai.profile_parser import Fact, ParseResult, apply_facts
from ..ai.provider import AIUnavailable
from ..crawler.client import CrawlerAuthError, CrawlerDisabled, CrawlerError
from ..crawler.models import JobDetail, JobOut
from ..db.models import BotUser, JobCache, SubscriptionMatch
from ..domain.criteria import CandidateProfile, SearchCriteria
from ..logging import get_logger
from ..questionnaire.convert import answers_to_profile, profile_to_answers
from ..questionnaire.engine import Button, Screen, result_profile
from .context import AppContext
from .keyboards import dislike_reasons, job_buttons, main_menu, start_menu
from .messenger import NO_PREVIEW, markup
from .render import explanation, job_card, profile_summary
from .texts import esc, t

log = get_logger(__name__)
THRESHOLDS = [85, 70, 0]
MODES = ["IMMEDIATE", "MORNING", "EVENING", "MANUAL"]
SAVED_STATUSES = ["applied", "interview", "rejected", "hired"]


def build_router(ctx: AppContext) -> Router:
    r = Router(name="vacancies-bot")
    s = ctx.settings

    # --- helpers -------------------------------------------------------------------------------------------------
    async def user_of(event: Message | CallbackQuery) -> BotUser:
        u = event.from_user
        chat_id = event.chat.id if isinstance(event, Message) else (event.message.chat.id if event.message else u.id)
        return await ctx.users.ensure(u.id, chat_id, u.language_code)

    async def send(bot: Bot, chat_id: int, text: str, rows: list[list[Button]] | None = None) -> Message:
        return await bot.send_message(chat_id, text, reply_markup=markup(rows), link_preview_options=NO_PREVIEW)

    async def show_screen(bot: Bot, user: BotUser, screen: Screen, draft: dict, *, cb: CallbackQuery | None = None) -> None:
        if cb and cb.message:
            try:
                await cb.message.edit_text(screen.text, reply_markup=markup(screen.rows), link_preview_options=NO_PREVIEW)
                draft["mid"] = cb.message.message_id
                return
            except Exception:  # message too old / identical → send a new one
                pass
        old = draft.get("mid")
        if old:
            try:
                await bot.edit_message_reply_markup(chat_id=user.chat_id, message_id=old, reply_markup=None)
            except Exception:
                pass
        msg = await send(bot, user.chat_id, screen.text, screen.rows)
        draft["mid"] = msg.message_id

    def crawler_error_text(lang: str, exc: Exception) -> str:
        if isinstance(exc, CrawlerAuthError | CrawlerDisabled):
            log.error("crawler_access_problem", error=str(exc))
            return t(lang, "error.crawler_auth")
        return t(lang, "error.crawler")

    def ai_needs_consent(user: BotUser) -> bool:
        return ctx.gateway.available and not user.ai_consent_version

    def consent_screen(lang: str, nxt: str) -> tuple[str, list[list[Button]]]:
        return (t(lang, "ai.consent", provider=esc(ctx.gateway.label)),
                [[Button(t(lang, "ai.consent_yes"), f"ai:y:{nxt}"), Button(t(lang, "ai.consent_no"), f"ai:n:{nxt}")]])

    # --- start / questionnaire -------------------------------------------------------------------------------------
    @r.message(CommandStart())
    async def cmd_start(m: Message, bot: Bot) -> None:
        user = await user_of(m)
        lang = user.ui_language
        rows = start_menu(lang)
        if await ctx.users.load_profile(user.id):
            rows += main_menu(lang)
        await send(bot, user.chat_id, t(lang, "start.hello"), rows)

    async def start_flow(bot: Bot, user: BotUser, mode: str, *, purpose: str = "profile", answers: dict | None = None,
                         cb: CallbackQuery | None = None) -> None:
        if answers is None and mode == "manual":
            prof = await ctx.users.load_profile(user.id)
            if prof:  # pre-fill filters from the profile
                a = profile_to_answers(prof)
                answers = {k: a[k] for k in ("target_roles", "skills", "countries", "work_models") if k in a}
        draft = ctx.engine.new_draft(mode, lang=user.ui_language, purpose=purpose, answers=answers)
        if mode == "manual" and answers:
            draft["step"] = "target_roles"
        out = await ctx.engine._screen(draft)
        await show_screen(bot, user, out.screen, draft, cb=cb)
        await ctx.users.save_draft(user.id, draft)

    @r.callback_query(F.data.startswith("st:"))
    async def cb_start(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        await cb.answer()
        await start_flow(bot, user, cb.data.split(":")[1], cb=cb)

    @r.callback_query(F.data.startswith("qn:"))
    async def cb_questionnaire(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        draft = await ctx.users.load_draft(user.id)
        await cb.answer()
        if not draft or "mode" not in draft:
            await send(bot, user.chat_id, t(user.ui_language, "cancelled"))
            return
        out = await ctx.engine.handle_callback(draft, cb.data)
        await after_engine(bot, user, out, cb=cb)

    async def after_engine(bot: Bot, user: BotUser, out, *, cb: CallbackQuery | None = None) -> None:
        draft = out.draft
        lang = draft.get("lang") or user.ui_language
        if out.kind == "cancelled":
            await ctx.users.drop_draft(user.id)
            if cb and cb.message:
                await cb.message.edit_text(t(lang, "cancelled"))
            return
        if out.kind == "done":
            await ctx.users.drop_draft(user.id)
            if cb and cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
            await finish_questionnaire(bot, user, draft)
            return
        await show_screen(bot, user, out.screen, draft, cb=cb)
        await ctx.users.save_draft(user.id, draft)

    async def finish_questionnaire(bot: Bot, user: BotUser, draft: dict) -> None:
        purpose = draft.get("purpose", "profile")
        if draft["mode"] == "manual":
            criteria = answers_to_profile(draft["a"], "manual").criteria
            prof = await ctx.users.load_profile(user.id)
            if prof:  # keep the rest of the profile (languages, exclusions…) for the one-off search
                base = prof.criteria.model_copy(update={k: getattr(criteria, k) for k in ("roles", "skills", "countries", "work_models")})
                criteria = base
            else:
                criteria.min_score = 0
            await run_and_show(bot, user, criteria, source="manual")
            return
        if purpose.startswith("sub:"):
            sub_id = int(purpose.split(":")[1])
            criteria = result_profile(draft).criteria
            await ctx.subs.replace_criteria(user.id, sub_id, criteria)
            await send(bot, user.chat_id, t(user.ui_language, "subs.updated"))
            return
        profile = result_profile(draft)
        version = await ctx.users.save_profile(user.id, profile)
        lang = profile.ui_language
        await send(bot, user.chat_id, t(lang, "profile.saved", v=version))
        user = await ctx.users.get(user.id)
        await run_and_show(bot, user, profile.criteria, source="profile")
        if profile.wants_cv_upload:
            await ask_cv(bot, user)

    @r.message(Command("profile"))
    async def cmd_profile(m: Message, bot: Bot) -> None:
        await show_profile(bot, await user_of(m))

    async def show_profile(bot: Bot, user: BotUser) -> None:
        lang = user.ui_language
        prof = await ctx.users.load_profile(user.id)
        if not prof:
            await send(bot, user.chat_id, t(lang, "no_profile"), start_menu(lang))
            return
        rows = [[Button(t(lang, "profile.edit"), "pr:edit")], [Button(t(lang, "profile.ai_text"), "pr:ai")],
                [Button(t(lang, "profile.cv"), "pr:cv")], [Button(t(lang, "search.by_profile"), "srch:profile")]]
        await send(bot, user.chat_id, profile_summary(prof, lang).rsplit("\n\n", 1)[0], rows)

    @r.callback_query(F.data.startswith("pr:"))
    async def cb_profile(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        await cb.answer()
        action = cb.data.split(":")[1]
        prof = await ctx.users.load_profile(user.id)
        if action == "edit":
            await start_flow(bot, user, "extended", answers=profile_to_answers(prof) if prof else None)
        elif action == "ai":
            await ask_profile_text(bot, user)
        elif action == "cv":
            await ask_cv(bot, user)

    # --- search ---------------------------------------------------------------------------------------------------
    @r.message(Command("search"))
    async def cmd_search(m: Message, bot: Bot) -> None:
        await search_menu(bot, await user_of(m))

    async def search_menu(bot: Bot, user: BotUser) -> None:
        lang = user.ui_language
        rows = [[Button(t(lang, "search.filters"), "srch:filters")], [Button(t(lang, "search.text"), "srch:text")]]
        if await ctx.users.load_profile(user.id):
            rows.insert(0, [Button(t(lang, "search.by_profile"), "srch:profile")])
        await send(bot, user.chat_id, t(lang, "search.choose"), rows)

    @r.callback_query(F.data.startswith("srch:"))
    async def cb_search(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        await cb.answer()
        what = cb.data.split(":")[1]
        if what == "profile":
            prof = await ctx.users.load_profile(user.id)
            if not prof:
                await send(bot, user.chat_id, t(user.ui_language, "no_profile"), start_menu(user.ui_language))
                return
            await run_and_show(bot, user, prof.criteria, source="profile")
        elif what == "filters":
            await start_flow(bot, user, "manual")
        elif what == "text":
            await ask_assistant(bot, user)

    async def run_and_show(bot: Bot, user: BotUser, criteria: SearchCriteria, *, source: str, intro: str | None = None) -> None:
        lang = user.ui_language
        await bot.send_chat_action(user.chat_id, "typing")
        try:
            sess, outcome = await ctx.subs.run_search(user.id, criteria, source=source)
        except CrawlerError as exc:
            await send(bot, user.chat_id, crawler_error_text(lang, exc))
            return
        threshold = criteria.min_score
        above = len(outcome.above(threshold))
        if not outcome.ranked:
            await send(bot, user.chat_id, (intro + "\n\n" if intro else "") + t(lang, "search.nothing"),
                       [[Button(t(lang, "search.filters"), "srch:filters")]])
            return
        head = t(lang, "search.found", n=above, c=outcome.candidates) if above else t(lang, "search.found_below", t=threshold)
        await send(bot, user.chat_id, (intro + "\n\n" if intro else "") + head)
        await show_page(bot, user, sess.id, threshold, source)

    async def show_page(bot: Bot, user: BotUser, sess_id: int, threshold: int, source: str) -> None:
        lang = user.ui_language
        items, more = await ctx.subs.take_page(user.id, sess_id, ctx.settings.initial_results_shown, threshold)
        for it in items:
            job = JobOut.model_validate(it["job"])
            await send(bot, user.chat_id, job_card(job, it["match"], lang), job_buttons(lang, job.id, job.url, it["score"], ctx=f"q{sess_id}"))
        rows = []
        if more:
            rows.append([Button(t(lang, "search.more"), f"ss:more:{sess_id}")])
        rows.append([Button(t(lang, "search.subscribe" if source == "profile" else "search.save_as_sub"), f"ss:sub:{sess_id}")])
        note = t(lang, "search.initial_note") if more else t(lang, "search.end") + " " + t(lang, "search.initial_note")
        await send(bot, user.chat_id, note, rows)

    @r.callback_query(F.data.startswith("ss:"))
    async def cb_session(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        await cb.answer()
        _, action, sid = cb.data.split(":")
        sess = await ctx.subs.get_session(user.id, int(sid))
        lang = user.ui_language
        if not sess:
            await send(bot, user.chat_id, t(lang, "error.generic"))
            return
        criteria = SearchCriteria.model_validate(sess.criteria)
        if action == "more":
            if cb.message:
                await cb.message.edit_reply_markup(reply_markup=None)
            await show_page(bot, user, sess.id, criteria.min_score, sess.source)
        elif action == "sub":
            prof = await ctx.users.load_profile(user.id)
            mode = prof.notifications.mode if prof else "IMMEDIATE"
            name = default_sub_name(criteria, lang)
            # a one-off search may run with threshold 0 ("show everything"); a subscription gets the balanced default
            min_score = 70 if sess.source != "profile" and criteria.min_score == 0 else None
            sub = await ctx.subs.create_from_session(user.id, sess.id, name=name, mode=mode, min_score=min_score,
                                                     profile_version=prof.profile_version if prof else None)
            if cb.message:
                await cb.message.edit_reply_markup(reply_markup=None)
            await send(bot, user.chat_id, t(lang, "subs.created", name=esc(sub.name)), main_menu(lang))

    def default_sub_name(c: SearchCriteria, lang: str) -> str:
        roles = ", ".join(r.name for r in c.roles[:2]) or ", ".join(s.name for s in c.skills[:2]) or "Jobs"
        where = ", ".join(c.countries) if c.countries else "DACH"
        return t(lang, "subs.name_default", roles=roles, where=where)[:120]

    # --- job cards: why / save / feedback --------------------------------------------------------------------------------
    async def job_and_match(user: BotUser, job_id: int, jctx: str) -> tuple[JobOut | None, dict | None, Any]:
        """(job, match json, holder) from a subscription match (``s<id>``) or a search session (``q<id>``)."""
        if jctx.startswith("q"):
            sess = await ctx.subs.get_session(user.id, int(jctx[1:]))
            for it in (sess.results if sess else []):
                if it["job_id"] == job_id:
                    return JobOut.model_validate(it["job"]), it["match"], sess
            return None, None, None
        sub_id = int(jctx[1:])
        sub = await ctx.subs.get(user.id, sub_id)
        if not sub:
            return None, None, None
        async with ctx.db.session() as ss:
            from sqlalchemy import select

            m = (await ss.execute(select(SubscriptionMatch).where(SubscriptionMatch.subscription_id == sub_id,
                                                                   SubscriptionMatch.job_id == job_id))).scalar_one_or_none()
            cache = await ss.get(JobCache, job_id)
        job = JobDetail.model_validate(cache.payload) if cache else None
        if job is None:
            try:
                job = await ctx.client.get_job(job_id)
            except CrawlerError:
                job = None
        return job, (m.data if m else None), m

    @r.callback_query(F.data.startswith("j:"))
    async def cb_job(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        parts = cb.data.split(":")
        action, job_id, jctx = parts[1], int(parts[2]), parts[3]
        lang = user.ui_language
        sub_id = int(jctx[1:]) if jctx.startswith("s") else None
        job, match, holder = await job_and_match(user, job_id, jctx)
        if job is None:
            await cb.answer(t(lang, "error.generic"))
            return
        if action == "w":
            await cb.answer()
            llm = await maybe_llm_explanation(user, job, match or {}, jctx, holder)
            await send(bot, user.chat_id, explanation(job, match or {}, lang, llm))
        elif action == "s":
            await ctx.feedback.save_job(user.id, job.id, job.title, job.company.name, job.url)
            await ctx.feedback.record(user.id, job.id, "save", sub_id=sub_id)
            await cb.answer(t(lang, "saved.added"))
        elif action == "a":
            await ctx.feedback.save_job(user.id, job.id, job.title, job.company.name, job.url, status="applied")
            await ctx.feedback.record(user.id, job.id, "applied", sub_id=sub_id)
            await cb.answer(t(lang, "saved.status_set"))
        elif action == "l":
            await ctx.feedback.record(user.id, job.id, "like", sub_id=sub_id)
            await cb.answer(t(lang, "fb.thanks"))
        elif action == "n":
            await cb.answer()
            await send(bot, user.chat_id, t(lang, "fb.why"), dislike_reasons(lang, job.id, jctx))
        elif action == "r":
            reason = parts[4]
            suggest = await ctx.feedback.record(user.id, job.id, "dislike", sub_id=sub_id, reason=reason)
            await cb.answer(t(lang, "fb.thanks"))
            if cb.message:
                await cb.message.edit_text(t(lang, "fb.adjusted") if sub_id else t(lang, "fb.thanks"))
            if suggest and sub_id:
                await send(bot, user.chat_id, t(lang, "fb.suggest_filter", reason=t(lang, f"fb.reason.{suggest}")),
                           [[Button(t(lang, "subs.edit"), f"sub:edit:{sub_id}")]])

    async def maybe_llm_explanation(user: BotUser, job: JobOut, match: dict, jctx: str, holder) -> dict | None:
        if not ctx.explainer or not user.ai_consent_version or not match:
            return None
        if isinstance(holder, SubscriptionMatch) and holder.llm_explanation:
            return holder.llm_explanation
        if jctx.startswith("q") and holder is not None:
            cached = next((it.get("llm") for it in holder.results if it["job_id"] == job.id and it.get("llm")), None)
            if cached:
                return cached
        criteria = None
        if isinstance(holder, SubscriptionMatch):
            sub = await ctx.subs.get(user.id, holder.subscription_id)
            criteria = SearchCriteria.model_validate(sub.criteria) if sub else None
        elif holder is not None:
            criteria = SearchCriteria.model_validate(holder.criteria)
        if criteria is None:
            return None
        try:
            llm = await ctx.explainer.explain(job, match, criteria, user.ui_language, user_id=user.id)
        except AIUnavailable:
            return None
        if llm:
            async with ctx.db.session() as ss:
                if isinstance(holder, SubscriptionMatch):
                    row = await ss.get(SubscriptionMatch, holder.id)
                    row.llm_explanation = llm
                elif holder is not None:
                    from ..db.models import SearchSession

                    row = await ss.get(SearchSession, holder.id)
                    row.results = [({**it, "llm": llm} if it["job_id"] == job.id else it) for it in row.results]
        return llm

    # --- subscriptions --------------------------------------------------------------------------------------------
    @r.message(Command("subscriptions"))
    async def cmd_subs(m: Message, bot: Bot) -> None:
        await list_subs(bot, await user_of(m))

    async def list_subs(bot: Bot, user: BotUser) -> None:
        lang = user.ui_language
        subs = await ctx.subs.list(user.id)
        if not subs:
            await send(bot, user.chat_id, t(lang, "subs.none"), [[Button(t(lang, "menu.search"), "m:search")]])
            return
        lines = [t(lang, "subs.title"), ""]
        rows = []
        for sub in subs:
            c = SearchCriteria.model_validate(sub.criteria)
            icon = "🔔" if sub.enabled else "⏸"
            lines.append(t(lang, "subs.item", icon=icon, name=esc(sub.name), mode=t(lang, f"opt.notification_mode.{sub.notification_mode}"),
                           score=c.min_score))
            rows.append([Button(f"{icon} {sub.name[:40]}", f"sub:open:{sub.id}")])
        await send(bot, user.chat_id, "\n".join(lines), rows)

    async def show_sub(bot: Bot, user: BotUser, sub_id: int, cb: CallbackQuery | None = None) -> None:
        lang = user.ui_language
        sub = await ctx.subs.get(user.id, sub_id)
        if not sub:
            await send(bot, user.chat_id, t(lang, "error.generic"))
            return
        c = SearchCriteria.model_validate(sub.criteria)
        prof = CandidateProfile(ui_language=lang if lang in ("ru", "uk", "de", "en") else "en", criteria=c)
        text = f"<b>{esc(sub.name)}</b> · v{sub.version}\n\n" + profile_summary(prof, lang, mode="manual").split("\n", 2)[2].rsplit("\n\n", 1)[0]
        text += f"\n\n🔔 {t(lang, f'opt.notification_mode.{sub.notification_mode}')} · {t(lang, 'label.min_score')}: {c.min_score}+"
        if sub.weights:
            text += "\n⚖️ " + ", ".join(f"{k}×{v}" for k, v in sub.weights.items())
        rows = [
            [Button(t(lang, "subs.pause" if sub.enabled else "subs.resume"), f"sub:{'pause' if sub.enabled else 'resume'}:{sub.id}"),
             Button(t(lang, "subs.updates_on" if sub.notify_updates else "subs.updates_off"), f"sub:upd:{sub.id}")],
            [Button(f"{t(lang, 'subs.threshold')}: {c.min_score}+", f"sub:thr:{sub.id}"), Button(t(lang, "subs.mode"), f"sub:mode:{sub.id}")],
            [Button(t(lang, "subs.edit"), f"sub:edit:{sub.id}"), Button(t(lang, "subs.delete"), f"sub:del:{sub.id}")],
        ]
        if sub.notification_mode == "MANUAL":
            rows.insert(0, [Button("📬 " + t(lang, "search.more"), f"sub:new:{sub.id}")])
        if cb and cb.message:
            try:
                await cb.message.edit_text(text, reply_markup=markup(rows), link_preview_options=NO_PREVIEW)
                return
            except Exception:
                pass
        await send(bot, user.chat_id, text, rows)

    @r.callback_query(F.data.startswith("sub:"))
    async def cb_sub(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        await cb.answer()
        _, action, sid = cb.data.split(":")
        sub_id = int(sid)
        lang = user.ui_language
        sub = await ctx.subs.get(user.id, sub_id)
        if not sub:
            return
        if action == "open":
            await show_sub(bot, user, sub_id)
            return
        if action in ("pause", "resume"):
            await ctx.subs.set_enabled(user.id, sub_id, action == "resume")
        elif action == "upd":
            await ctx.subs.update(user.id, sub_id, notify_updates=not sub.notify_updates)
        elif action == "thr":
            cur = SearchCriteria.model_validate(sub.criteria).min_score
            nxt = THRESHOLDS[(THRESHOLDS.index(cur) + 1) % len(THRESHOLDS)] if cur in THRESHOLDS else 70
            await ctx.subs.update(user.id, sub_id, min_score=nxt)
        elif action == "mode":
            nxt = MODES[(MODES.index(sub.notification_mode) + 1) % len(MODES)]
            await ctx.subs.update(user.id, sub_id, notification_mode=nxt)
        elif action == "del":
            await ctx.subs.delete(user.id, sub_id)
            if cb.message:
                await cb.message.edit_text(t(lang, "subs.deleted"))
            return
        elif action == "edit":
            c = SearchCriteria.model_validate(sub.criteria)
            answers = profile_to_answers(CandidateProfile(ui_language=lang if lang in ("ru", "uk", "de", "en") else "en", criteria=c))
            await start_flow(bot, user, "extended", purpose=f"sub:{sub_id}", answers=answers)
            return
        elif action == "new":
            for mrow in await ctx.subs.pending_matches(user.id):
                job, match, _ = await job_and_match(user, mrow.job_id, f"s{mrow.subscription_id}")
                if job:
                    await send(bot, user.chat_id, job_card(job, match or {}, lang),
                               job_buttons(lang, job.id, job.url, (match or {}).get("score", 0), ctx=f"s{mrow.subscription_id}"))
            return
        await show_sub(bot, user, sub_id, cb)

    # --- saved -------------------------------------------------------------------------------------------------------
    @r.message(Command("saved"))
    async def cmd_saved(m: Message, bot: Bot) -> None:
        await list_saved(bot, await user_of(m))

    async def list_saved(bot: Bot, user: BotUser) -> None:
        lang = user.ui_language
        items = await ctx.feedback.saved(user.id)
        if not items:
            await send(bot, user.chat_id, t(lang, "saved.none"))
            return
        lines = [t(lang, "saved.title"), ""]
        rows = []
        for i, it in enumerate(items[:15], 1):
            closed = " ❌" if it.job_closed else ""
            lines.append(f"{i}. <a href=\"{esc(it.url)}\">{esc(it.title)}</a> — {esc(it.company)} · {t(lang, f'saved.status.{it.status}')}{closed}")
            rows.append([Button(f"{i}. {t(lang, f'saved.status.{st}')[:2]}", f"sv:{it.job_id}:{st}") for st in SAVED_STATUSES])
        await send(bot, user.chat_id, "\n".join(lines), rows[:10])

    @r.callback_query(F.data.startswith("sv:"))
    async def cb_saved(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        _, job_id, status = cb.data.split(":")
        if status in SAVED_STATUSES:
            await ctx.feedback.set_status(user.id, int(job_id), status)
        await cb.answer(t(user.ui_language, "saved.status_set"))

    # --- privacy / deletion ---------------------------------------------------------------------------------------------
    @r.message(Command("privacy"))
    async def cmd_privacy(m: Message, bot: Bot) -> None:
        await show_privacy(bot, await user_of(m))

    async def show_privacy(bot: Bot, user: BotUser) -> None:
        lang = user.ui_language
        if ctx.gateway.available:
            state = t(lang, "privacy.ai_state_on" if user.ai_consent_version else "privacy.ai_state_off")
            ai = t(lang, "privacy.ai_on", provider=esc(ctx.gateway.label), state=state)
        else:
            ai = t(lang, "privacy.ai_off")
        rows = [[Button(t(lang, "delete.yes"), "del:ask")]]
        if user.ai_consent_version:
            rows.insert(0, [Button(t(lang, "privacy.revoke"), "ai:revoke:-")])
        rows.insert(0, [Button(t(lang, "cv.delete"), "cv:wipe")])
        await send(bot, user.chat_id, t(lang, "privacy.text", cv_h=s.cv_retention_hours, ai=ai), rows)

    @r.message(Command("delete_me"))
    async def cmd_delete(m: Message, bot: Bot) -> None:
        user = await user_of(m)
        lang = user.ui_language
        await send(bot, user.chat_id, t(lang, "delete.confirm"), [[Button(t(lang, "delete.yes"), "del:yes"), Button(t(lang, "delete.no"), "del:no")]])

    @r.callback_query(F.data.startswith("del:"))
    async def cb_delete(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        lang = user.ui_language
        await cb.answer()
        action = cb.data.split(":")[1]
        if action == "ask":
            await send(bot, user.chat_id, t(lang, "delete.confirm"), [[Button(t(lang, "delete.yes"), "del:yes"), Button(t(lang, "delete.no"), "del:no")]])
        elif action == "yes":
            await ctx.users.delete_everything(user.id)
            if cb.message:
                await cb.message.edit_text(t(lang, "delete.done"))
        elif cb.message:
            await cb.message.edit_text(t(lang, "cancelled"))

    # --- AI: consent, profile text, assistant, facts confirmation ------------------------------------------------------------
    async def ask_profile_text(bot: Bot, user: BotUser) -> None:
        if ai_needs_consent(user):
            text, rows = consent_screen(user.ui_language, "ptext")
            await send(bot, user.chat_id, text, rows)
            return
        await ctx.users.save_draft(user.id, {"await": "profile_text"})
        await send(bot, user.chat_id, t(user.ui_language, "ai.ask_profile_text"))

    async def ask_assistant(bot: Bot, user: BotUser) -> None:
        if ai_needs_consent(user):
            text, rows = consent_screen(user.ui_language, "ask")
            await send(bot, user.chat_id, text, rows)
            return
        await ctx.users.save_draft(user.id, {"await": "assistant"})
        await send(bot, user.chat_id, t(user.ui_language, "ai.ask_text"))

    @r.callback_query(F.data.startswith("ai:"))
    async def cb_ai(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        await cb.answer()
        _, answer, nxt = cb.data.split(":", 2)
        if answer == "revoke":
            await ctx.users.set_ai_consent(user.id, None)
            await send(bot, user.chat_id, t(user.ui_language, "privacy.revoked"))
            return
        if answer == "y":
            await ctx.users.set_ai_consent(user.id, ctx.settings.ai_consent_version)
        else:
            await ctx.users.set_ai_consent(user.id, None)
        if cb.message:
            await cb.message.edit_reply_markup(reply_markup=None)
        user = await ctx.users.get(user.id)
        state = {"ptext": "profile_text", "ask": "assistant"}.get(nxt)
        if state:
            await ctx.users.save_draft(user.id, {"await": state, "ai": answer == "y"})
            await send(bot, user.chat_id, t(user.ui_language, "ai.ask_profile_text" if state == "profile_text" else "ai.ask_text"))

    def facts_screen(lang: str, pr: ParseResult, sel: list[int], *, source: str) -> tuple[str, list[list[Button]]]:
        lines = [t(lang, "ai.parsed")]
        if not pr.facts:
            return t(lang, "ai.parsed_none"), []
        rows = []
        for i, f in enumerate(pr.facts):
            label = fact_label(f, lang)
            mark = "✅" if i in sel else "▫️"
            lines.append(f"{mark} {esc(label)} — «{esc(f.evidence[:60])}»")
            rows.append([Button(f"{mark} {label}"[:60], f"pf:t:{i}")])
        if pr.question:
            lines.append("\n" + t(lang, "ai.question", q=esc(pr.question)))
        rows = rows[:24]
        rows.append([Button(t(lang, "ai.apply"), "pf:ok"), Button(t(lang, "ai.discard"), "pf:no")])
        return "\n".join(lines), rows

    def fact_label(f: Fact, lang: str) -> str:
        v = f.value
        kind = t(lang, f"ai.f.{f.kind}") if f.kind != "remote_from" else t(lang, "label.remote_from")
        if f.kind == "language":
            val = f"{t(lang, 'opt.lang.' + v['code'], default=v['code'])} {v.get('level') or '?'}"
        elif f.kind == "experience":
            val = t(lang, f"opt.experience.{v['bucket']}")
        elif f.kind in ("country", "remote_from"):
            val = t(lang, f"opt.country.{v['code']}")
        elif f.kind == "work_model":
            val = t(lang, f"opt.work_models.{v['value']}")
        elif f.kind == "seniority":
            val = t(lang, f"opt.preferred_seniority.{v['value']}", default=v["value"].title())
        else:
            val = str(v.get("name") or v.get("value") or v)
        src = t(lang, "ai.source.cv" if f.source == "cv" else "ai.source.text")
        return f"{kind}: {val} ({src}{', AI' if f.by == 'ai' else ''})"

    async def show_facts(bot: Bot, user: BotUser, pr: ParseResult, source: str) -> None:
        sel = list(range(len(pr.facts)))
        await ctx.users.save_draft(user.id, {"await": "facts", "pr": pr.to_json(), "sel": sel, "source": source})
        text, rows = facts_screen(user.ui_language, pr, sel, source=source)
        await send(bot, user.chat_id, text, rows or None)

    @r.callback_query(F.data.startswith("pf:"))
    async def cb_facts(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        await cb.answer()
        state = await ctx.users.load_draft(user.id)
        lang = user.ui_language
        if not state or state.get("await") != "facts":
            return
        pr = ParseResult.from_json(state["pr"])
        parts = cb.data.split(":")
        if parts[1] == "t":
            i = int(parts[2])
            sel = set(state["sel"])
            sel.symmetric_difference_update({i})
            state["sel"] = sorted(sel)
            await ctx.users.save_draft(user.id, state)
            text, rows = facts_screen(lang, pr, state["sel"], source=state["source"])
            if cb.message:
                await cb.message.edit_text(text, reply_markup=markup(rows))
            return
        await ctx.users.drop_draft(user.id)
        if state["source"] == "cv":
            await ctx.cv.wipe(user.id, confirmed=parts[1] == "ok")  # delete the CV text right after the review
        if parts[1] == "no":
            if cb.message:
                await cb.message.edit_text(t(lang, "cancelled"))
            return
        chosen = [pr.facts[i] for i in state["sel"] if i < len(pr.facts)]
        prof = await ctx.users.load_profile(user.id)
        answers = profile_to_answers(prof) if prof else {"ui_language": lang}
        answers, n = apply_facts(answers, chosen)
        profile = answers_to_profile(answers, "extended")
        if prof:
            profile.experience_summary = prof.experience_summary
            profile.notifications = prof.notifications
        version = await ctx.users.save_profile(user.id, profile)
        if cb.message:
            await cb.message.edit_reply_markup(reply_markup=None)
        await send(bot, user.chat_id, t(lang, "ai.applied", n=n) + "\n" + t(lang, "profile.saved", v=version))
        # open the summary so the user can complete/adjust the rest
        await start_flow(bot, user, "extended", answers=profile_to_answers(profile))

    # --- CV ------------------------------------------------------------------------------------------------------------
    async def ask_cv(bot: Bot, user: BotUser) -> None:
        lang = user.ui_language
        where = t(lang, "cv.where_ai", provider=esc(ctx.gateway.label)) if ctx.gateway.available else t(lang, "cv.where_local")
        await send(bot, user.chat_id, t(lang, "cv.consent", where=where, h=s.cv_retention_hours),
                   [[Button(t(lang, "ai.consent_yes"), "cv:yes"), Button(t(lang, "ai.consent_no"), "cv:no")]])

    @r.message(Command("cv"))
    async def cmd_cv(m: Message, bot: Bot) -> None:
        await ask_cv(bot, await user_of(m))

    @r.callback_query(F.data.startswith("cv:"))
    async def cb_cv(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        await cb.answer()
        lang = user.ui_language
        action = cb.data.split(":")[1]
        if action == "wipe":
            await ctx.cv.wipe(user.id)
            await ctx.users.set_cv_consent(user.id, False)
            await send(bot, user.chat_id, t(lang, "cv.deleted"))
            return
        if cb.message:
            await cb.message.edit_reply_markup(reply_markup=None)
        if action == "yes":
            await ctx.users.set_cv_consent(user.id, True)
            await ctx.users.save_draft(user.id, {"await": "cv"})
            await send(bot, user.chat_id, t(lang, "cv.ask", mb=s.cv_max_bytes // (1024 * 1024)))
        else:
            await send(bot, user.chat_id, t(lang, "cancelled"))

    @r.message(F.document)
    async def on_document(m: Message, bot: Bot) -> None:
        user = await user_of(m)
        lang = user.ui_language
        state = await ctx.users.load_draft(user.id)
        if not state or state.get("await") != "cv" or not user.cv_consent_at:
            await ask_cv(bot, user)
            return
        doc = m.document
        if doc.file_size and doc.file_size > s.cv_max_bytes:
            await send(bot, user.chat_id, t(lang, "cv.too_big", mb=s.cv_max_bytes // (1024 * 1024)))
            return
        name = (doc.file_name or "").lower()
        if not (name.endswith(".pdf") or name.endswith(".docx")):
            await send(bot, user.chat_id, t(lang, "cv.bad_type"))
            return
        buf = io.BytesIO()
        await bot.download(doc, destination=buf)
        from ..ai.cv import CvError

        try:
            text, sha = ctx.cv.prepare(buf.getvalue(), doc.mime_type or "", name)
        except CvError:
            await send(bot, user.chat_id, t(lang, "cv.failed"))
            return
        finally:
            buf.close()
        await bot.send_chat_action(user.chat_id, "typing")
        pr = await ctx.parser.parse(text[: s.cv_max_chars_to_ai], user_id=user.id, use_ai=ctx.gateway.available, source="cv")
        if ctx.gateway.available and pr.ai_error:
            await send(bot, user.chat_id, t(lang, "ai.unavailable"))
        await ctx.cv.remember(user.id, sha, doc.mime_type or "", doc.file_size or 0, text, pr.to_json())
        await show_facts(bot, user, pr, "cv")

    # --- menu / misc ------------------------------------------------------------------------------------------------------
    @r.message(Command("help"))
    async def cmd_help(m: Message, bot: Bot) -> None:
        user = await user_of(m)
        await send(bot, user.chat_id, t(user.ui_language, "help"), main_menu(user.ui_language))

    @r.callback_query(F.data.startswith("m:"))
    async def cb_menu(cb: CallbackQuery, bot: Bot) -> None:
        user = await user_of(cb)
        await cb.answer()
        what = cb.data.split(":")[1]
        handlers = {"search": search_menu, "subs": list_subs, "profile": show_profile, "saved": list_saved, "privacy": show_privacy,
                    "ask": ask_assistant}
        if what in handlers:
            await handlers[what](bot, user)

    @r.callback_query(F.data == "noop")
    async def cb_noop(cb: CallbackQuery) -> None:
        await cb.answer()

    @r.message(F.text)
    async def on_text(m: Message, bot: Bot) -> None:
        user = await user_of(m)
        lang = user.ui_language
        state = await ctx.users.load_draft(user.id)
        text = (m.text or "").strip()
        if state and "mode" in state:  # questionnaire waiting for text
            out = await ctx.engine.handle_text(state, text)
            await after_engine(bot, user, out)
            return
        waiting = (state or {}).get("await")
        use_ai = bool(ctx.gateway.available and user.ai_consent_version)
        if waiting == "profile_text":
            await ctx.users.drop_draft(user.id)
            await bot.send_chat_action(user.chat_id, "typing")
            pr = await ctx.parser.parse(text[:1500], user_id=user.id, use_ai=use_ai)
            if use_ai and pr.ai_error:
                await send(bot, user.chat_id, t(lang, "ai.unavailable"))
            prof = await ctx.users.load_profile(user.id)
            if prof:
                prof.experience_summary = text[:1500]
                await ctx.users.save_profile(user.id, prof)
            await show_facts(bot, user, pr, "text")
            return
        if waiting in ("cv", "facts"):
            await send(bot, user.chat_id, t(lang, "q.use_buttons"))
            return
        # anything else: a free-text search request (manual search; subscriptions are not touched)
        await ctx.users.drop_draft(user.id)
        await bot.send_chat_action(user.chat_id, "typing")
        prof = await ctx.users.load_profile(user.id)
        criteria, pr = await ctx.assistant.interpret(text[:600], prof, user_id=user.id, use_ai=use_ai)
        if not pr.facts:
            await send(bot, user.chat_id, t(lang, "ai.parsed_none"), start_menu(lang))
            return
        intro = t(lang, "ai.interpreted") + "\n" + "\n".join(f"• {esc(fact_label(f, lang))}" for f in pr.facts[:12])
        if use_ai and pr.ai_error:
            intro = t(lang, "ai.unavailable") + "\n\n" + intro
        await run_and_show(bot, user, criteria, source="assistant", intro=intro)

    return r
