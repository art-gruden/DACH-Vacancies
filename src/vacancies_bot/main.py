"""Entry points.

    vacancies-bot run              # Telegram polling + background workers
    vacancies-bot migrate          # alembic upgrade head
    vacancies-bot check-crawler    # verify token, routes and response schemas against the live crawler
    vacancies-bot demo             # offline demo (fake crawler, optional fake AI) incl. AI call accounting
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from .config import get_settings
from .logging import get_logger, setup_logging

log = get_logger(__name__)


async def _run() -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.types import BotCommand

    from .bot.context import build_context
    from .bot.handlers import build_router
    from .bot.messenger import AiogramMessenger
    from .workers import run_workers

    settings = get_settings()
    if not settings.bot_token.get_secret_value():
        sys.exit("BOT_TOKEN is not set")
    if not settings.crawler_api_token.get_secret_value():
        log.warning("CRAWLER_API_TOKEN is empty: the crawler will answer 401/503 until a key is configured")
    ctx = build_context(settings)
    bot = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(build_router(ctx))
    await bot.set_my_commands([
        BotCommand(command=c, description=d) for c, d in (
            ("start", "Start / questionnaire"), ("profile", "Profile"), ("search", "Search"), ("subscriptions", "Subscriptions"),
            ("saved", "Saved vacancies"), ("cv", "Upload CV"), ("privacy", "Privacy"), ("delete_me", "Delete my data"), ("help", "Help"))
    ])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    workers = asyncio.create_task(run_workers(ctx, AiogramMessenger(bot), stop))
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    await stop.wait()
    await dp.stop_polling()
    await asyncio.gather(workers, polling, return_exceptions=True)
    await ctx.client.aclose()
    await ctx.db.dispose()
    await bot.session.close()


async def _check() -> int:
    from .crawler.client import CrawlerClient, CrawlerError, JobQuery

    st = get_settings()
    c = CrawlerClient(st.crawler_base_url, st.crawler_api_token.get_secret_value(), timeout=st.crawler_timeout_s)
    ok = True
    for name, call in (
        ("facets/countries", c.countries), ("facets/regions?country=DE", lambda: c.regions("DE")),
        ("facets/cities?country=DE", lambda: c.cities("DE", limit=5)), ("facets/specializations", c.specializations),
        ("facets/skills", lambda: c.skills(limit=5)), ("jobs?limit=1", lambda: c.search_jobs(JobQuery(), limit=1)),
        ("changes/head", c.changes_head), ("changes?limit=1", lambda: c.changes(None, limit=1)),
    ):
        try:
            res = await call()
            n = len(res) if isinstance(res, list) else (len(res.items) if hasattr(res, "items") else 1)
            print(f"OK   {name}: {n}")
        except CrawlerError as exc:
            ok = False
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        except Exception as exc:  # schema mismatch
            ok = False
            print(f"FAIL {name}: response does not match the contract: {exc}")
    await c.aclose()
    return 0 if ok else 1


def _migrate() -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")


def cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="vacancies-bot")
    p.add_argument("command", choices=["run", "migrate", "check-crawler", "demo"])
    p.add_argument("--with-ai", action="store_true", help="demo: use a scripted fake AI provider")
    args = p.parse_args(argv)
    setup_logging(get_settings().log_level)
    if args.command == "run":
        asyncio.run(_run())
    elif args.command == "migrate":
        _migrate()
    elif args.command == "check-crawler":
        sys.exit(asyncio.run(_check()))
    elif args.command == "demo":
        from .demo import main as demo_main

        asyncio.run(demo_main(with_ai=args.with_ai))


if __name__ == "__main__":
    cli()
