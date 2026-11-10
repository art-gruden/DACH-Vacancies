# Критерии приёмки (раздел 11 спецификации)

| # | Критерий | Реализация | Проверка |
|---|---|---|---|
| 1 | без CV и LLM: подписка, поиск, уведомления | `AI_PROVIDER=none` по умолчанию; правила везде | `test_bot_e2e::test_quick_questionnaire_search_subscribe_notify`, `test_free_text_request_without_ai` |
| 2 | справочники из facets или явный свободный ввод | `questionnaire/engine.py` (`_r_spec/_r_skills/_r_regions/_r_cities`), `free:` варианты, `canonical=False` | `test_questionnaire::test_quick_flow…`, `test_facets_unavailable_falls_back_to_text` |
| 3 | исправить AI-интерпретацию до активации | экран фактов с галочками, затем карточка профиля «Изменить» | `test_ai_consent_profile_parsing_and_cached_explanation`, `test_confirmed_facts_only_are_applied` |
| 4 | hard-ограничения соблюдаются, AI их не ослабляет | `hard_filter` после API; реранкер не может пропустить отсеянное; обратная связь меняет только мягкие веса | `test_reranker_cannot_bypass_hard_filters`, `test_feedback_multipliers…`, e2e (критерии подписки не меняются) |
| 5 | неизвестное ≠ «не подходит» | `include_unknown`, `unknowns`, `completeness` | `test_remote_path_and_unknowns_are_not_mismatches`, `test_salary_strict_and_unknown` |
| 6 | несколько стран/городов/навыков/языков, независимые подписки | списки в `SearchCriteria`, `subscriptions` | `test_same_job_in_two_subscriptions_is_one_message`, `test_query_plan_free_text_role_and_regions` |
| 7 | начальная выдача не рассылается | `origin=initial` | `test_initial_results_not_resent_and_gap_events_not_lost` |
| 8 | события между поиском и активацией не теряются | baseline до поиска, водяной знак подписки | тот же тест (вакансия 20 создана между предпросмотром и подпиской) |
| 9 | сбои и рестарты без массовых дублей | атомарный курсор, уникальные ключи, аренда отправки | `test_crash_between_page_and_cursor_is_idempotent`, `test_ambiguous_send_is_not_repeated`, `test_crawler_outage…` |
| 10 | изменения и закрытия без спама | `job.updated` по опции, `job.closed` отменяет | `test_manual_mode_and_update_opt_in`, `test_closed_vacancy_cancels_pending_and_marks_saved` |
| 11 | объяснения, пробелы, полнота | `MatchResult.why/gaps/unknowns/checks/completeness`, «Почему N?» | `test_score_explanations`, e2e |
| 12 | удаление, хранение, согласие на AI | `/delete_me`, `/privacy`, `CvStore`, версия согласия | `test_delete_me_removes_everything`, `test_cv_pipeline_scrubs_and_deletes`, e2e (отзыв согласия) |
| 13 | API-параметры документированы по OpenAPI | `docs/crawler-api-contract.md`, `contracts/` | `test_contract.py` |
| 14 | DE/AT/CH, RU/DE/EN, CV без ПДн, пустые facets, запреты, зарплата unknown, CEFR, режимы уведомлений | фикстуры fake_crawler (CH/AT/DE), тексты RU/EN | `test_matching.py`, `test_ai.py`, `test_feed.py::test_quiet_hours_daily_limit_and_digest` |
| 15 | экономный расход AI | LLM только по кнопке + кэш, эмбеддинги по хешу | `test_semantic_rerank_is_cached…`, `vacancies-bot demo --with-ai` |

Частично: п. 14 «DE» как язык интерфейса — тексты UK/DE пока частичные (остальное на EN), см. assumptions.md.
