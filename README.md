# DACH Job Bot 2.0 — AI Career Assistant (Telegram)

Отдельный Telegram Bot Service поверх **Germany Career Crawler Bot API** (`/api/bot/v1`). Помогает сформулировать
профиль, ищет вакансии в DE/AT/CH, объясняет соответствие и присылает новые вакансии по персональным подпискам.
Crawler не меняется и не получает никаких данных пользователей.

* **Анкета** по спецификации (33 шага): быстрый режим (≈15 вопросов), расширенный, «просто поиск по фильтрам».
  Справочники (специализации, навыки, регионы, города, страны) — из актуальных `/facets/*`, всегда есть свободный ввод.
* **Поиск**: план запросов к `/jobs` по реальным параметрам → локальные hard-фильтры → объяснимая оценка 0–100.
* **Подписки**: несколько независимых, с собственными условиями, порогом, частотой и опцией «уведомлять об обновлениях».
* **Надёжная лента**: `/changes` + `/changes/head`, durable inbox, атомарный курсор, водяные знаки подписок,
  outbox с дедупликацией, тихие часы, дневной лимит, сводки. Начальная выдача не рассылается как «новое».
* **AI (необязательно)**: разбор текста «о себе» и CV, поиск обычным текстом, семантическое переранжирование,
  объяснения «Почему N?». Всё по отдельному согласию, с проверкой «каждый факт — дословная цитата», с бюджетом.
  Без AI работает всё, кроме LLM-пояснений (их заменяет объяснение по правилам).

## Быстрый старт

```bash
cp .env.example .env            # BOT_TOKEN, BOT_DB_PASSWORD, CRAWLER_API_TOKEN, (опц.) CV_ENCRYPTION_KEY
docker compose up -d --build    # bot-db (PostgreSQL 16) + bot: alembic upgrade head → polling + воркеры
docker compose logs -f bot
```

### Токен crawler (обязательно)
Сейчас `https://crawdler.duckdns.org/api/bot/v1/*` отвечает **503 «Bot API is disabled»**: на сервере crawler не задан
`BOT_API_KEYS`. Код crawler менять не нужно, только конфигурацию:

```bash
# на сервере crawler (/opt/Crawdler)
docker compose exec api python -m gcc_core.botauth generate telegram
# → строку "telegram:sha256:<hex>" добавить в .env crawler как BOT_API_KEYS=..., затем
docker compose up -d api
# → напечатанный TOKEN записать в .env бота как CRAWLER_API_TOKEN
docker compose run --rm bot vacancies-bot check-crawler   # проверка маршрутов и схем ответов по контракту
```

Логин/пароль администратора дашборда crawler боту **не нужны** и нигде не хранятся: Bot API принимает только
Bearer-токен из `BOT_API_KEYS`.

### Локально (без Docker)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                    # SQLite, без сети
BOT_TEST_PG_URL=postgresql+asyncpg://postgres@localhost/bot_test pytest -q   # те же тесты + PostgreSQL
vacancies-bot demo --with-ai                 # офлайн-демо: расход запросов к crawler и AI-вызовов
DATABASE_URL=... alembic upgrade head && vacancies-bot run
```

## Команды бота
`/start` · `/profile` · `/search` · `/subscriptions` · `/saved` · `/cv` · `/privacy` · `/delete_me` · `/help`.
Любой текст вне анкеты — поисковый запрос («SRE удалённо из Германии, English C1, без Werkstudent»): бот покажет,
как понял запрос, и выполнит **ручной** поиск; подписки меняются только кнопкой «Сохранить как подписку».

## Документация
| Файл | Содержание |
|---|---|
| [docs/crawler-api-contract.md](docs/crawler-api-contract.md) | реальные параметры и схемы Bot API (по OpenAPI) и как бот их использует |
| [docs/architecture.md](docs/architecture.md) | компоненты, поток событий, гарантии доставки, AI-конвейер, приватность |
| [docs/phases.md](docs/phases.md) | что сделано в каждой фазе и результаты тестов |
| [docs/acceptance.md](docs/acceptance.md) | критерии приёмки 1–15 → реализация и тесты |
| [docs/migration-plan.md](docs/migration-plan.md) | миграции БД, порядок деплоя, откат |
| [docs/assumptions.md](docs/assumptions.md) | допущения, ограничения, что осталось на P2 |
| [.env.example](.env.example) | все настройки конфигурации |

## Структура

```
contracts/crawler-bot-v1.openapi.json   снимок контракта (9 маршрутов /api/bot/v1 + схемы)
scripts/regenerate_client.sh            обновить снимок с живого crawler и перегенерировать модели
src/vacancies_bot/
  crawler/      models_generated.py (из OpenAPI), client.py (auth, ретраи, 429, 409/400, пагинация)
  domain/       criteria.py (модель профиля), matching.py (hard-фильтры, оценка, причины), query_builder.py
  questionnaire/ steps.py (33 шага как данные), engine.py (FSM без Telegram), convert.py
  services/     facets, search, subscriptions, users, feedback
  feed/         ingest.py (лента → inbox), processor.py (события → совпадения → outbox)
  notify/       planner.py (тихие часы, сводки), sender.py (outbox → Telegram)
  ai/           provider.py (шлюз, бюджет), profile_parser.py, search_assistant.py, cv.py, semantic.py, explain.py
  bot/          handlers.py (aiogram 3), render.py, texts.py (RU/EN), keyboards.py, messenger.py
  testing/      fake_crawler.py, fake_ai.py (для тестов и демо)
migrations/     Alembic (0001 — схема Bot Service)
tests/          95 тестов (SQLite + PostgreSQL)
```
