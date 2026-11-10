# Crawler Bot API — фактический контракт и его использование ботом

Источник: живой `https://crawdler.duckdns.org/api/openapi.json` (прочитан 2026-11-10) и репозиторий `art-gruden/crawdler`
(коммит `4566a17`, `docs/openapi.json`, `career_crawler/bot_api.py`, `gcc_core/botauth.py`). Снимок подмножества —
`contracts/crawler-bot-v1.openapi.json`; модели `src/vacancies_bot/crawler/models_generated.py` сгенерированы из него
(`datamodel-code-generator`). `tests/test_contract.py` проверяет, что клиент использует только объявленные параметры,
константы совпадают с перечислениями из описаний, а поля моделей — со схемами.

## Аутентификация и ошибки
* В OpenAPI схема безопасности **не объявлена**. По коду: `Authorization: Bearer <token>`, ключи — `BOT_API_KEYS`
  (`name:sha256:<hex>`), токен генерирует `python -m gcc_core.botauth generate <name>`.
* Состояние на 2026-11-10: все `/api/bot/v1/*` отвечают **503 `{"detail":"Bot API is disabled"}`** — ключ не настроен.
* 401 — неверный токен (`CrawlerAuthError`, без ретраев) · 422 — неизвестный навык/страна/чужой курсор (`CrawlerValidationError`, без ретраев) ·
  429 — ждём `Retry-After` (≤ 60 с), затем повтор · 503 «disabled» → `CrawlerDisabled` · прочие 5xx/сеть → экспоненциальный backoff.

## GET `/jobs` — поиск
Фильтры объединяются через AND, значения одного фильтра — через OR. Неизвестное значение не проходит фильтр,
если не указано в `include_unknown`. Пагинация keyset: `limit` (1–100, по умолч. 25), `cursor` (непрозрачный, привязан к запросу).

| Параметр | Тип / значения | Как использует бот |
|---|---|---|
| `q` | строка ≤ 200, `"фразы"`, `-слово` | свободные формулировки роли (`"Platform Engineer" or sre`) |
| `exclude` | список слов | **не используется**: исключения применяются локально к названию и компании (описание дало бы ложные срабатывания) |
| `country` | ISO 3166-1: DE, AT, CH | страны анкеты |
| `region` | ISO 3166-2: DE-SN, AT-9, CH-ZH | регионы (отдельный запрос) |
| `city` | id из `/facets/cities` | города (отдельный запрос) |
| `specialization` | slug или id; slug группы раскрывается в её специализации | выбранные специализации; группы — в режиме BROAD |
| `work_model` | REMOTE, HYBRID, ONSITE | формат + `include_unknown=work_model` |
| `employment` | FULL_TIME, PART_TIME, CONTRACT, FREELANCE, TEMPORARY, INTERNSHIP, APPRENTICESHIP, WORKING_STUDENT, OTHER | занятость + `include_unknown=employment` |
| `seniority` | INTERN, JUNIOR, MID, SENIOR, LEAD, EXECUTIVE | **не используется** как фильтр (crawler определяет уровень по названию) — только ранжирование |
| `skills`, `skills_mode` | имена из `/facets/skills`; any/all | доп. запрос «смежные роли» по основным навыкам (BALANCED/BROAD); 422 на неизвестный навык → эта часть плана пропускается |
| `languages`, `lang_mode` | `de:B1,en:C1`; inclusive/strict | только при «Нет» в вопросе 19 (inclusive). При WARN/YES язык проверяется локально, чтобы показать «exceeds» с предупреждением |
| `salary_min`, `currency`, `salary_period` | число; ISO 4217 (обязательна); year/month/hour, без конвертации | только строгая зарплата (+ `include_unknown=salary` по выбору) |
| `new_since` | ISO 8601 | ресинхронизация после сброса курсора (409/400) |
| `employer` | id компаний | не используется |
| `status` | open/closed/expired/all | по умолчанию open |
| `remote_in` | страны проживания удалёнщика | отдельный запрос `work_model=REMOTE&remote_in=<страна>&include_unknown=remote` |
| `include_unknown` | location, work_model, employment, seniority, specialization, salary, remote | см. выше; `location` — только в BROAD |
| `sort` | newest, relevance, salary_desc | `relevance` |

Ответ `JobPage {items: JobOut[], next_cursor, has_more}`. `JobOut`: `id, title, company{id,name,domain,country}, url,
application_url, status, published_at (только из источника), first_seen_at, last_seen_at, last_checked_at, updated_at, closed_at,
geo_status, locations[{id,country,region,city,…,is_primary,confidence,source}], work_model (+UNKNOWN),
remote_eligibility{scope: unknown|worldwide|europe|eu|dach|countries, countries[]}, employment_types[], specialization,
secondary_specializations[], seniority (+UNKNOWN), skills[{name,category}], languages[{language,level,requirement,descriptor,evidence,confidence}],
salary{min,max,currency,period,basis,raw,confidence}, quality{score,completeness,missing[],field_confidence}, language_fit, relevance`.

## GET `/jobs/{job_id}` — `JobDetail` = `JobOut` + `description, requirements[], responsibilities[]`; 404 → `None`.
Бот запрашивает его для событий ленты (один раз на вакансию за проход, кэш `job_cache`).

## Facets
| Маршрут | Параметры | Ответ | В боте |
|---|---|---|---|
| `/facets/countries` | — | `code, name, name_local, in_dach, open_jobs, companies` | вопрос 10 (с количеством) |
| `/facets/regions` | `country` (обяз.) | `code, country, name, name_local, kind, open_jobs` | вопрос 11 |
| `/facets/cities` | `country` (обяз.), `region`, `q` (префикс ≤ 60, ä=ae), `limit` ≤ 500 | `id, city, country, region, open_jobs` | вопрос 12, поиск по вводу |
| `/facets/specializations` | — | дерево групп: `id, slug, name, name_de, is_it, open_jobs, children[]` | вопросы 02 и 20 |
| `/facets/skills` | `q` (префикс ≤ 40), `limit` ≤ 200 | `name, category, open_jobs` | вопрос 06, канонизация навыков из текста/CV |

Кэш: память + таблица `facet_cache` (TTL `FACETS_TTL_S`); при недоступности crawler — устаревшие данные, иначе свободный ввод.

## Лента изменений
* `GET /changes?cursor&limit≤500` → `ChangesPage {items: ChangeItem[], next_cursor, has_more}`;
  `ChangeItem {event_id, event_type: job.created|job.updated|job.closed|job.reopened, job_id, occurred_at, fields (updated), reason (closed: missed|expired)}`.
  At-least-once, порядок коммита, повторное чтение с тем же курсором даёт те же события.
* Без курсора — с начала журнала; 400 — битый курсор; 409 — курсор впереди журнала (БД crawler восстановлена).
* `GET /changes/head` → `{cursor}` — «сейчас».

Бот: первый запуск — курсор из `/changes/head`; страница событий и новый курсор сохраняются в одной транзакции;
обработка идемпотентна по `event_id`; при 400/409 — курсор из `/changes/head`, флаг `resync_since` и догоняющий поиск
`/jobs?new_since=…` для каждой активной подписки.

## Не используется
`/api/jobs*`, `/api/companies*`, `/api/crawler*`, `/api/auth/*` (дашборд с логином администратора) — это не Bot API.
