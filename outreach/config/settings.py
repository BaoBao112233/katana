"""
config/settings.py
──────────────────────────────────────────────────────
Central configuration via Pydantic Settings + .env file.
All secrets loaded from environment variables — never hardcode.

Usage:
    from config.settings import settings
    print(settings.groq_api_key)
"""

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────
    app_name: str = "Company Database"
    app_version: str = "0.1.0"
    debug: bool = False
    log_level: str = "INFO"

    # ── Database (PostgreSQL) ────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/companydb",
        description="Async PostgreSQL DSN",
    )
    # Sync DSN for Alembic migrations
    database_url_sync: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/companydb",
    )

    # ── Redis ────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # ── Elasticsearch ────────────────────────────────────────────────
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "companies"

    # ── AI / Groq ────────────────────────────────────────────────────
    groq_api_key: str = Field(default="", description="Groq API key from console.groq.com")
    groq_model: str = "openai/GPT-OSS-20B"           # adjust to exact Groq model id
    groq_temperature: float = 0.0
    groq_max_tokens: int = 1024
    ai_batch_size: int = 10                            # domains per AI batch
    ai_max_html_chars: int = 4000                      # truncate HTML before sending to LLM

    # ── Collectors ───────────────────────────────────────────────────
    google_api_key: str = ""
    google_cx: str = ""                               # Custom Search Engine ID
    google_maps_api_key: str = ""

    # ── Crawler ──────────────────────────────────────────────────────
    playwright_headless: bool = True
    playwright_timeout_ms: int = 30_000
    crawler_concurrency: int = 10
    crawler_depth: int = 2
    crawler_rate_limit_rps: float = 2.0               # requests per second per worker

    # ── Proxy ────────────────────────────────────────────────────────
    proxy_list_path: Optional[str] = None             # path to newline-separated proxy file
    proxy_rotation_enabled: bool = False

    # ── Enrichment APIs ──────────────────────────────────────────────
    hunter_api_key: str = ""
    apollo_api_key: str = ""
    clearbit_api_key: str = ""

    # ── SaaS API ─────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_secret_key: str = "change-me-in-production-please"
    api_allowed_origins: list[str] = ["*"]

    # ── Common Crawl ─────────────────────────────────────────────────
    common_crawl_index: str = "CC-MAIN-2024-51"       # latest crawl index
    common_crawl_max_domains: int = 1_000_000

    # ── Kafka (production only) ───────────────────────────────────────
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_raw_domains: str = "raw_domains"
    kafka_topic_to_crawl: str = "to_crawl"
    kafka_topic_crawled: str = "crawled"
    use_kafka: bool = False                            # False = use Redis/Celery (MVP mode)


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()


# Module-level singleton for convenience
settings = get_settings()
