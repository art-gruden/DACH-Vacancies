"""End-to-end through the real aiogram Dispatcher with a fake Telegram session (no network)."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User
from sqlalchemy import select

from vacancies_bot.bot.context import build_context
from vacancies_bot.bot.handlers import build_router
from vacancies_bot.bot.messenger import AiogramMessenger
from vacancies_bot.config import Settings
from vacancies_bot.db.models import BotUser, Subscription
from vacancies_bot.notify.sender import Sender
from vacancies_bot.testing.fake_crawler import L, make_job

UID = 777
_ids = itertools.count(1)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.mid = itertools.count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name == "SendMessage":
            return Message(message_id=next(self.mid), date=datetime.now(UTC), chat=Chat(id=method.chat_id, type="private"), text=method.text)
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    async def close(self):
        pass

    def texts(self):
        return [c.text for c in self.calls if type(c).__name__ in ("SendMessage", "EditMessageText")]

    def buttons(self):
        out = []
        for c in self.calls:
            rm = getattr(c, "reply_markup", None)
            if rm is not None and hasattr(rm, "inline_keyboard"):
                out += [b for row in rm.inline_keyboard for b in row]
        return out


def user():
    return User(id=UID, is_bot=False, first_name="Dima", language_code="ru")


def msg_update(text: str) -> Update:
    ents = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if text.startswith("/") else None
    return Update(update_id=next(_ids), message=Message(message_id=next(_ids), date=datetime.now(UTC), chat=Chat(id=UID, type="private"),
                                                        from_user=user(), text=text, entities=ents))


def cb_update(data: str) -> Update:
    m = Message(message_id=next(_ids), date=datetime.now(UTC), chat=Chat(id=UID, type="private"), text="x")
    return Update(update_id=next(_ids), callback_query=CallbackQuery(id=str(next(_ids)), from_user=user(), chat_instance="c", data=data, message=m))


@pytest.fixture
async def app(db, client, fake):
    settings = Settings(bot_token="1:x", database_url=db.url, crawler_api_token="t", feed_catchup_budget_s=1)
    ctx = build_context(settings, db=db, client=client)
    session = FakeSession()
    bot = Bot("123:abc", session=session)
    dp = Dispatcher()
    dp.include_router(build_router(ctx))

    class App:
        pass

    a = App()
    a.ctx, a.bot, a.dp, a.session, a.fake = ctx, bot, dp, session, fake

    async def send(text):
        await dp.feed_update(bot, msg_update(text))

    async def press(data):
        await dp.feed_update(bot, cb_update(data))

    async def pick(value):
        draft = await ctx.users.load_draft(UID)
        idx = next(i for i, (v, _) in enumerate(draft["opts"]) if v == value)
        await press(f"qn:o:{idx}")

    a.send, a.press, a.pick = send, press, pick
    return a


async def test_quick_questionnaire_search_subscribe_notify(app):
    await app.send("/start")
    assert "Привет" in app.session.texts()[-1]
    await app.press("st:quick")
    await app.pick("ru")
    await app.press("qn:g:1")
    await app.pick("devops-sre")
    await app.press("qn:d")
    await app.pick("6-10")
    await app.send("kub")  # typed search inside the skills step
    await app.pick("Kubernetes")
    await app.send("terra")
    await app.pick("Terraform")
    await app.press("qn:d")
    for v in ("DE", "CH"):
        await app.pick(v)
    await app.press("qn:d")
    for v in ("REMOTE", "HYBRID"):
        await app.pick(v)
    await app.press("qn:d")
    await app.pick("DE")  # remote from
    await app.pick("FULL_TIME")
    await app.press("qn:d")
    await app.pick("de")
    await app.pick("en")
    await app.press("qn:d")
    await app.pick("B2")
    await app.pick("C1")
    await app.pick("WARN")
    await app.pick("70")
    await app.pick("IMMEDIATE")
    await app.press("qn:ok")
    texts = app.session.texts()
    assert any("Профиль сохранён" in x for x in texts)
    assert any("Найдено" in x for x in texts)
    assert any("Site Reliability Engineer" in x for x in texts)
    sub_btn = next(b for b in app.session.buttons() if b.callback_data and b.callback_data.startswith("ss:sub:"))
    await app.press(sub_btn.callback_data)
    assert "Подписка" in app.session.texts()[-1]
    async with app.ctx.db.session() as s:
        sub = (await s.execute(select(Subscription))).scalar_one()
        u = await s.get(BotUser, UID)
        u.quiet_start = u.quiet_end = None
    # a new vacancy appears in the crawler → notification through aiogram
    app.fake.add_job(make_job(40, "SRE / Platform (m/w/d)", company="Spree Cloud GmbH", city=1, work_model="REMOTE",
                              remote_scope="countries", remote_countries=["DE"], employment=["FULL_TIME"], seniority="SENIOR",
                              skills=["Kubernetes", "Terraform"], languages=[L("en", "C1")]))
    await app.ctx.ingestor.poll()
    await app.ctx.processor.run_once()
    stats = await Sender(app.ctx.db, AiogramMessenger(app.bot)).run_once(datetime.now(UTC) + timedelta(seconds=1))
    assert stats.items == 1 and "Spree Cloud GmbH" in app.session.texts()[-1]
    # explanation, save, dislike with reason (soft weight changes, hard constraints don't)
    await app.press(f"j:w:40:s{sub.id}")
    assert "Почему" in app.session.texts()[-1] and "Kubernetes" in app.session.texts()[-1]
    await app.press(f"j:s:40:s{sub.id}")
    await app.press(f"j:r:40:s{sub.id}:language")
    async with app.ctx.db.session() as s:
        fresh = await s.get(Subscription, sub.id)
        assert fresh.weights == {"language": 1.15} and fresh.criteria == sub.criteria
    await app.send("/saved")
    assert "SRE / Platform" in app.session.texts()[-1]
    await app.send("/subscriptions")
    assert "Ваши подписки" in app.session.texts()[-1]
    await app.press(f"sub:thr:{sub.id}")
    async with app.ctx.db.session() as s:
        assert (await s.get(Subscription, sub.id)).criteria["min_score"] == 0
    await app.send("/delete_me")
    await app.press("del:yes")
    assert "удалены" in app.session.texts()[-1]
    async with app.ctx.db.session() as s:
        assert await s.get(BotUser, UID) is None and (await s.execute(select(Subscription))).first() is None


async def test_free_text_request_without_ai(app):
    await app.send("/start")
    await app.send("Нужна работа SRE удалённо из Германии, английский C1, немецкий B2, желательно Terraform и Kubernetes, без Werkstudent")
    texts = app.session.texts()
    assert any("Понял запрос так" in x for x in texts)
    joined = "\n".join(texts)
    assert "Werkstudent DevOps" not in joined  # excluded
    assert "Kubernetes" in joined and ("DevOps Engineer" in joined or "Site Reliability" in joined)
    assert app.ctx.gateway.calls == 0  # no AI configured → rules only
    async with app.ctx.db.session() as s:
        assert (await s.execute(select(Subscription))).first() is None  # a manual search never creates a subscription


async def test_crawler_down_message(app):
    app.fake.down = True
    await app.send("/start")
    await app.send("DevOps Kubernetes remote")
    assert "недоступна" in app.session.texts()[-1]


async def test_privacy_and_help(app):
    await app.send("/privacy")
    assert "Crawler" in app.session.texts()[-1] and "не подключена" in app.session.texts()[-1]
    await app.send("/help")
    assert "/delete_me" in app.session.texts()[-1]


@pytest.fixture
async def app_ai(db, client, fake):
    from vacancies_bot.testing.fake_ai import FakeAIProvider

    settings = Settings(bot_token="1:x", database_url=db.url, crawler_api_token="t", feed_catchup_budget_s=1,
                        ai_provider_label="Fake AI")
    provider = FakeAIProvider()
    ctx = build_context(settings, db=db, client=client, ai_provider=provider)
    session = FakeSession()
    bot = Bot("123:abc", session=session)
    dp = Dispatcher()
    dp.include_router(build_router(ctx))

    class App:
        pass

    a = App()
    a.ctx, a.bot, a.dp, a.session, a.fake, a.ai = ctx, bot, dp, session, fake, provider

    async def send(text):
        await dp.feed_update(bot, msg_update(text))

    async def press(data):
        await dp.feed_update(bot, cb_update(data))

    a.send, a.press = send, press
    return a


async def test_ai_consent_profile_parsing_and_cached_explanation(app_ai):
    app = app_ai
    await app.send("/start")
    await app.press("m:ask")
    assert "Fake AI" in app.session.texts()[-1] and app.ai.chat_calls == 0  # consent first, nothing sent yet
    await app.press("ai:y:ask")
    await app.send("SRE remote из Германии, Kubernetes и Terraform, English C1")
    assert app.ai.chat_calls == 1 and any("Понял запрос так" in x for x in app.session.texts())
    # parse a profile text; untick one fact before saving
    await app.press("pr:ai")
    await app.send("SRE, 6 years: Kubernetes, Helm, Terraform. German is fine, English C1.")
    state = await app.ctx.users.load_draft(UID)
    assert state["await"] == "facts"
    facts = state["pr"]["facts"]
    assert not any(f["value"].get("name") == "Rust" for f in facts)  # the model's invention never reaches the user
    helm = next(i for i, f in enumerate(facts) if f["value"].get("name") == "Helm")
    await app.press(f"pf:t:{helm}")
    await app.press("pf:ok")
    prof = await app.ctx.users.load_profile(UID)
    names = {s.name for s in prof.criteria.skills}
    assert "Kubernetes" in names and "Helm" not in names
    assert any("Сохранено в профиль" in x for x in app.session.texts())
    # "Why N?" → one grounded LLM explanation, then served from the cache
    await app.press("srch:profile")
    why = next(b.callback_data for b in reversed(app.session.buttons()) if b.callback_data and b.callback_data.startswith("j:w:"))
    calls = app.ai.chat_calls
    await app.press(why)
    assert app.ai.chat_calls == calls + 1 and "Пояснение AI" in app.session.texts()[-1]
    assert "150000" not in app.session.texts()[-1]
    await app.press(why)
    assert app.ai.chat_calls == calls + 1  # cached
    await app.press("ai:revoke:-")
    await app.press(why)
    assert "Пояснение AI" not in app.session.texts()[-1]  # without consent: rules only
