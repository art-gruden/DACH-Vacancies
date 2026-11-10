# План миграции и развёртывания

Бот — новый отдельный сервис со своей БД; существующие функции crawler не затрагиваются, схема crawler не меняется.

Если бот ставится на сервер crawler, шаги 1 и 2.1 выполняет `./install.sh --crawler-dir /opt/Crawdler`.

## 1. Crawler (только конфигурация, 5 минут)
1. `docker compose exec api python -m gcc_core.botauth generate telegram`
2. В `.env` crawler: `BOT_API_KEYS=telegram:sha256:<hex>`; при необходимости `BOT_RATE_LIMIT_PER_MIN`.
3. `docker compose up -d api` → `/api/bot/v1/facets/countries` с токеном отвечает 200, без токена 401.

## 2. Бот
1. `./install.sh` (или `cp .env.example .env` и заполнить `BOT_TOKEN`, `BOT_DB_PASSWORD`, `CRAWLER_API_TOKEN`, опц. `CV_ENCRYPTION_KEY`).
2. `docker compose up -d --build` — контейнер сам выполняет `alembic upgrade head` (ревизия `0001`, 16 таблиц), затем запускает бота.
3. `docker compose run --rm bot vacancies-bot check-crawler` — все строки `OK`.
4. Первый цикл ленты возьмёт курсор из `/changes/head`: история crawler не рассылается.

Ресурсы на 2 CPU / 4 GB вместе с crawler: `bot` ≤ 384 МБ, `bot-db` ≤ 256 МБ (`shared_buffers=64MB`). Нагрузка на crawler:
лента — 1 запрос/мин в простое; поиск — до 6 запросов `/jobs` + детали по событиям.

## 3. Миграции БД
| Ревизия | Содержание | Откат |
|---|---|---|
| `0001` | `bot_users, candidate_profiles, questionnaire_drafts, subscriptions, subscription_matches, saved_jobs, feedback, crawler_event_inbox, crawler_sync_cursor, job_cache, notification_outbox, search_sessions, cv_documents, ai_usage, embedding_cache, facet_cache` | `alembic downgrade base` (удаляет всё) |

Новые изменения: `alembic revision --autogenerate -m "…"` → проверить файл → `alembic upgrade head`; `alembic check` в CI.

## 4. Обновление контракта crawler
`./scripts/regenerate_client.sh` → новый снимок и модели → `pytest tests/test_contract.py`. Если crawler добавит поля —
модели принимают их без ошибок (лишние поля игнорируются), тест покажет расхождение для осознанного обновления.

## 5. Откат бота
`docker compose stop bot` (crawler не зависит от бота). Данные остаются в `bot-db`; при повторном запуске лента продолжит с
сохранённого курсора, а при слишком старом курсоре (409) — ресинхронизация через `/jobs?new_since`.
