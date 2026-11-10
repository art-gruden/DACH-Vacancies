"""Runtime configuration (environment variables / .env). Secrets are never logged."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram -------------------------------------------------------------------------------
    bot_token: SecretStr = SecretStr("")
    admin_ids: list[int] = Field(default_factory=list)
    default_language: Literal["ru", "uk", "de", "en"] = "ru"

    # --- storage ----------------------------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://bot:bot@localhost:5432/vacancies_bot"

    # --- crawler Bot API (read-only, Bearer token from the crawler's BOT_API_KEYS) ------------------
    crawler_base_url: str = "https://crawdler.duckdns.org/api/bot/v1"
    crawler_api_token: SecretStr = SecretStr("")
    crawler_timeout_s: float = 20.0
    crawler_max_retries: int = 3
    facets_ttl_s: int = 900

    # --- feed / notifications ---------------------------------------------------------------------
    feed_poll_interval_s: int = 60
    feed_page_size: int = 200
    feed_catchup_budget_s: float = 8.0  # how long a preview may wait for the feed to catch up
    processor_interval_s: int = 15
    sender_interval_s: int = 5
    inbox_retention_days: int = 14
    event_max_attempts: int = 8
    send_lease_s: int = 120
    digest_threshold: int = 3  # >= this many pending items for a user → one combined message
    morning_digest_hour: int = 8
    evening_digest_hour: int = 19
    initial_results_shown: int = 5
    candidates_per_query: int = 100

    # --- AI (optional; everything works without it) -----------------------------------------------
    ai_provider: Literal["none", "openai_compat"] = "none"
    ai_provider_label: str = "external AI provider"  # shown to users in consent texts
    ai_base_url: str = "http://localhost:11434/v1"  # e.g. Ollama, vLLM, OpenAI-compatible gateways
    ai_api_key: SecretStr = SecretStr("")
    ai_chat_model: str = "qwen2.5:7b-instruct"
    ai_embed_model: str = ""  # empty → semantic reranking disabled
    ai_timeout_s: float = 60.0
    ai_chat_extra: dict = Field(default_factory=dict)  # merged into chat requests, e.g. {"reasoning_effort": "low"}
    ai_monthly_budget_usd: float = 5.0
    ai_daily_user_calls: int = 30
    ai_price_in_per_1k: float = 0.0
    ai_price_out_per_1k: float = 0.0
    ai_explain_top_k: int = 3
    ai_semantic_weight: float = 0.2  # share of the semantic component in the final score
    ai_consent_version: int = 1

    # --- CV ---------------------------------------------------------------------------------------
    cv_encryption_key: SecretStr = SecretStr("")  # Fernet key; empty → extracted text is never stored
    cv_max_bytes: int = 5 * 1024 * 1024
    cv_retention_hours: int = 24
    cv_max_chars_to_ai: int = 12000

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
