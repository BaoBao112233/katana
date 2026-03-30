"""
workers/celery_app.py
──────────────────────────────────────────────────────
Celery application + configuration.
Broker: Redis  |  Backend: Redis

Production scaling:
  - Run multiple workers: celery -A workers.celery_app worker --concurrency=16
  - Separate queues for crawl vs AI vs enrichment
  - Monitor: celery flower

Usage:
    from workers.celery_app import celery_app
"""

from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

from config.settings import settings

# ── App ────────────────────────────────────────────────────────────────────────

celery_app = Celery(
    "company_crawler",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["workers.tasks"],
)

# ── Configuration ──────────────────────────────────────────────────────────────

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Timezone
    timezone="UTC",
    enable_utc=True,

    # Task behavior
    task_acks_late=True,                    # ack only after task completes
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,           # don't prefetch — fair scheduling

    # Result expiry
    result_expires=3600,                    # 1 hour

    # Retry defaults
    task_max_retries=3,
    task_default_retry_delay=60,            # seconds

    # Routing — separate queues by function
    task_routes={
        "workers.tasks.task_crawl_domain": {"queue": "crawl"},
        "workers.tasks.task_ai_classify":  {"queue": "ai"},
        "workers.tasks.task_enrich":       {"queue": "enrich"},
        "workers.tasks.task_collect_google_maps": {"queue": "collect"},
        "workers.tasks.task_collect_common_crawl": {"queue": "collect"},
    },
)

# ── Queues ─────────────────────────────────────────────────────────────────────

default_exchange = Exchange("default", type="direct")

celery_app.conf.task_queues = (
    Queue("collect", default_exchange, routing_key="collect"),
    Queue("crawl",   default_exchange, routing_key="crawl"),
    Queue("ai",      default_exchange, routing_key="ai"),
    Queue("enrich",  default_exchange, routing_key="enrich"),
)
celery_app.conf.task_default_queue = "crawl"

# ── Beat schedule (periodic tasks) ─────────────────────────────────────────────

celery_app.conf.beat_schedule = {
    # Collect from Google Maps every 6 hours
    "collect-google-maps": {
        "task": "workers.tasks.task_collect_google_maps",
        "schedule": crontab(minute=0, hour="*/6"),
        "args": (["software company", "fintech startup"], ""),
    },
    # Process pending domains every 30 minutes
    "process-pending": {
        "task": "workers.tasks.task_dispatch_pending_domains",
        "schedule": crontab(minute="*/30"),
    },
}
