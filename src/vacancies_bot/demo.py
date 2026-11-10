"""Offline demo: how many crawler and AI calls a realistic day costs.

    python -m vacancies_bot.main demo            # deterministic only
    python -m vacancies_bot.main demo --with-ai  # with the scripted fake AI provider

Scenario: 20 users subscribe (each after an initial search), the crawler emits 60 change events
(40 new vacancies, 10 updates, 10 closures), the workers run, 5 users press "Why N?" (twice each).
"""

from __future__ import annotations

import random
import tempfile
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from .bot.context import build_context
from .config import Settings
from .crawler.client import CrawlerClient
from .db.models import AiUsage, BotUser, NotificationOutbox
from .db.session import Database
from .domain.criteria import LanguageRef, RoleRef, SearchCriteria, SkillRef
from .notify.sender import Sender
from .testing.fake_ai import FakeAIProvider
from .testing.fake_crawler import TOKEN, FakeCrawler, L, make_job

ROLES = [("devops-sre", "DevOps / SRE", ["Kubernetes", "Terraform"]), ("backend-development", "Backend development", ["Java", "Python"]),
         ("data-engineering", "Data engineering", ["Python", "Spark"])]


class _Messenger:
    def __init__(self) -> None:
        self.sent = 0

    async def send(self, chat_id, text, rows=None) -> int:
        self.sent += 1
        return self.sent


async def main(with_ai: bool = False) -> None:
    rnd = random.Random(7)
    tmp = tempfile.mkdtemp()
    db = Database(f"sqlite+aiosqlite:///{tmp}/demo.db")
    await db.create_all()
    fake = FakeCrawler()
    client = CrawlerClient("https://crawler.demo/api/bot/v1", TOKEN, transport=fake.transport)
    provider = FakeAIProvider() if with_ai else None
    ctx = build_context(Settings(database_url=db.url, crawler_api_token="x", feed_catchup_budget_s=2), db=db, client=client,
                        ai_provider=provider)
    await ctx.ingestor.poll()
    for uid in range(1, 21):
        slug, name, skills = ROLES[uid % 3]
        await ctx.users.ensure(uid, uid, "ru")
        async with db.session() as s:
            u = await s.get(BotUser, uid)
            u.quiet_start = u.quiet_end = None
            u.ai_consent_version = 1 if with_ai and uid % 2 else None
        c = SearchCriteria(roles=[RoleRef(name=name, slug=slug, group="information-technology")],
                           skills=[SkillRef(name=x, core=True) for x in skills], countries=["DE", "AT", "CH"],
                           languages=[LanguageRef(code="en", level="C1"), LanguageRef(code="de", level="B2")], min_score=60)
        sess, _ = await ctx.subs.run_search(uid, c)
        await ctx.subs.create_from_session(uid, sess.id, name=name, mode="IMMEDIATE")
    req_after_subscribe = sum(fake.requests.values())
    for i in range(40):
        slug, name, skills = ROLES[i % 3]
        fake.add_job(make_job(1000 + i, f"{name} #{i}", company=f"Firma {i} GmbH", city=rnd.choice([1, 2, 5, 7, 9]), spec=slug,
                              work_model=rnd.choice(["HYBRID", "REMOTE", "ONSITE"]), employment=["FULL_TIME"], seniority="MID",
                              skills=skills + rnd.sample(["Docker", "Linux", "AWS", "Go"], 2), languages=[L("en", "B2")], age_days=0))
    for i in range(10):
        fake.update_job(1000 + i, title=f"{fake.jobs[1000 + i]['title']} (updated)")
    for i in range(10, 20):
        fake.close_job(1000 + i)
    messenger = _Messenger()
    sender = Sender(db, messenger, digest_threshold=3)
    await ctx.ingestor.poll()
    await ctx.processor.run_once()
    stats = await sender.run_once(datetime.now(UTC) + timedelta(seconds=1))
    llm_before = provider.chat_calls if provider else 0
    explained = 0
    if ctx.explainer:
        from .crawler.models import JobDetail
        from .db.models import JobCache, Subscription, SubscriptionMatch

        async with db.session() as s:
            rows = (await s.execute(select(SubscriptionMatch, Subscription).join(Subscription).where(
                SubscriptionMatch.origin == "feed", SubscriptionMatch.passed.is_(True)).limit(5))).all()
        for m, sub in rows:
            async with db.session() as s:
                cache = await s.get(JobCache, m.job_id)
            job = JobDetail.model_validate(cache.payload)
            for _ in range(2):  # the second click is served from the stored explanation
                async with db.session() as s:
                    row = await s.get(SubscriptionMatch, m.id)
                    if row.llm_explanation:
                        continue
                    row.llm_explanation = await ctx.explainer.explain(job, row.data, SearchCriteria.model_validate(sub.criteria), "ru",
                                                                      user_id=sub.user_id) or {"points": []}
                explained += 1
    async with db.session() as s:
        outbox = dict((await s.execute(select(NotificationOutbox.status, func.count()).group_by(NotificationOutbox.status))).all())
        ai_rows = (await s.execute(select(func.count()).select_from(AiUsage))).scalar()
        cost = (await s.execute(select(func.coalesce(func.sum(AiUsage.cost_usd), 0.0)))).scalar()
    print("=== DACH Job Bot 2.0 — offline demo ===")
    print("users/subscriptions:            20 / 20")
    print(f"crawler requests (subscribe):   {req_after_subscribe}  (facets + query plans + feed)")
    print(f"crawler requests (total):       {sum(fake.requests.values())}  {dict(fake.requests)}")
    print("change events:                  60 (40 created, 10 updated, 10 closed)")
    print(f"notification outbox:            {outbox}")
    print(f"telegram messages sent:         {stats.sent_messages} (items: {stats.items}; digests merge several items)")
    if provider:
        print(f"LLM calls for matching 20×60:   {llm_before}  (matching is deterministic)")
        print(f"LLM calls for 10 'Why?' clicks: {provider.chat_calls - llm_before}  (5 unique, cached afterwards; {explained} generated)")
        print(f"embedding requests / texts:     {provider.embed_calls} / {provider.embedded_texts}  (cached per vacancy content hash)")
        print(f"ai_usage rows / cost:           {ai_rows} / ${cost:.4f}")
    else:
        print("AI:                             disabled — search, subscriptions and notifications work with rules only")
    await client.aclose()
    await db.dispose()
