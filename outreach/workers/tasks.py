"""
workers/tasks.py
──────────────────────────────────────────────────────
Celery task definitions for the full pipeline.

Pipeline flow per domain:
  1. task_collect_*           → add raw domains to DB
  2. task_crawl_domain        → Playwright crawl → store html/text
  3. task_ai_classify         → LangChain+Groq → classify + extract
  4. task_enrich              → Hunter.io → find emails
  5. task_dispatch_pending    → periodic: pick up new domains

Run workers:
  # All queues on one machine (MVP):
  celery -A workers.celery_app worker -Q crawl,ai,enrich,collect --concurrency=8

  # Production: separate workers per queue:
  celery -A workers.celery_app worker -Q crawl   --concurrency=16 --hostname=crawler@%h
  celery -A workers.celery_app worker -Q ai      --concurrency=4  --hostname=ai@%h
  celery -A workers.celery_app worker -Q enrich  --concurrency=8  --hostname=enrich@%h
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from celery import shared_task
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai.company_detector import CompanyDetector
from config.settings import settings
from collectors.google_maps import GoogleMapsCollector
from collectors.common_crawl import CommonCrawlCollector
from crawler.data_extractor import DataExtractor
from crawler.playwright_crawler import PlaywrightCrawler
from utils.domain_utils import normalize_domain, domain_to_url

log = logging.getLogger(__name__)

# Module-level singletons (reused across tasks within the same worker process)
_detector: Optional[CompanyDetector] = None
_extractor = DataExtractor()


def _get_detector() -> CompanyDetector:
    global _detector
    if _detector is None:
        _detector = CompanyDetector()
    return _detector


# ── DB session helper ──────────────────────────────────────────────────────────

def _get_sync_session():
    """
    Return a synchronous SQLAlchemy session for use inside Celery tasks.
    (Celery tasks run in a sync context; we use sync engine here.)
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(settings.database_url_sync, pool_pre_ping=True)
    return Session(engine)


# ── Helper: run async in sync context ─────────────────────────────────────────

def _run_async(coro):
    """Run an async coroutine from a sync Celery task."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ── Task 1: Collect from Google Maps ──────────────────────────────────────────

@shared_task(
    name="workers.tasks.task_collect_google_maps",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def task_collect_google_maps(
    self,
    queries: list[str],
    location: str = "",
    max_per_query: int = 200,
) -> dict:
    """Collect companies from Google Maps and insert into DB."""
    from db.models import Company

    log.info("Collecting from Google Maps: queries=%s location=%s", queries, location)

    collector = GoogleMapsCollector()

    async def _collect():
        results = []
        async for company in collector.collect_batch_queries(
            queries, location=location, max_per_query=max_per_query
        ):
            results.append(company)
        return results

    try:
        companies = _run_async(_collect())
    except Exception as exc:
        log.error("Google Maps collection failed: %s", exc)
        raise self.retry(exc=exc)

    inserted = 0
    with _get_sync_session() as session:
        for c in companies:
            if not c.domain:
                continue
            stmt = pg_insert(Company).values(
                domain=c.domain,
                name=c.name,
                url=c.url,
                phone=c.phone,     # stored in metadata
                address=c.address,  # stored in metadata
                source=c.source,
                external_id=c.external_id,
                crawl_status="pending",
                metadata={"phone": c.phone, "address": c.address, "lat": c.lat, "lng": c.lng},
            ).on_conflict_do_nothing(index_elements=["domain"])
            result = session.execute(stmt)
            if result.rowcount:
                inserted += 1
        session.commit()

    log.info("Google Maps: inserted %d new domains", inserted)
    return {"inserted": inserted, "total": len(companies)}


# ── Task 2: Crawl a domain ────────────────────────────────────────────────────

@shared_task(
    name="workers.tasks.task_crawl_domain",
    bind=True,
    max_retries=3,
    default_retry_delay=120,
    time_limit=120,           # hard kill after 2 min
    soft_time_limit=90,
)
def task_crawl_domain(self, domain: str) -> dict:
    """Playwright-crawl a domain and update DB. Then trigger AI classification."""
    from db.models import Company, CrawlJob

    log.info("Crawling: %s", domain)
    url = domain_to_url(domain)

    async def _crawl():
        async with PlaywrightCrawler(
            headless=settings.playwright_headless,
            timeout_ms=settings.playwright_timeout_ms,
        ) as crawler:
            return await crawler.crawl(url, follow_links=["/about", "/contact"])

    try:
        page = _run_async(_crawl())
    except Exception as exc:
        log.error("Playwright crash for %s: %s", domain, exc)
        raise self.retry(exc=exc)

    extracted = _extractor.extract(page)

    with _get_sync_session() as session:
        session.execute(
            update(Company)
            .where(Company.domain == domain)
            .values(
                crawl_status="done" if not page.error else "failed",
                crawled_at=datetime.now(timezone.utc),
                http_status=page.status_code,
                title=page.title,
                meta_description=page.meta_description,
                crawl_error=page.error,
                social_links=page.social_links,
                heuristic_score=len(extracted.signals) * 10,
            )
        )
        session.commit()

        # Fetch company id for job log
        row = session.execute(
            select(Company.id).where(Company.domain == domain)
        ).scalar_one_or_none()

        if row:
            job = CrawlJob(
                company_id=row,
                status="done" if not page.error else "failed",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                duration_ms=page.crawl_time_ms,
                error_message=page.error,
                worker_host=self.request.hostname,
            )
            session.add(job)
            session.commit()

    # Chain: trigger AI classification
    if not page.error:
        task_ai_classify.apply_async(
            args=[domain, page.html, page.title, page.meta_description],
            queue="ai",
        )

    return {
        "domain": domain,
        "status": "done" if not page.error else "failed",
        "emails_found": len(page.emails),
        "signals": extracted.signals,
    }


# ── Task 3: AI Classify ───────────────────────────────────────────────────────

@shared_task(
    name="workers.tasks.task_ai_classify",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    time_limit=60,
)
def task_ai_classify(
    self,
    domain: str,
    html: str = "",
    title: str = "",
    meta_description: str = "",
) -> dict:
    """Use LangChain+Groq to classify and enrich company info."""
    from db.models import Company

    log.info("AI classifying: %s", domain)
    detector = _get_detector()

    try:
        result = _run_async(detector.detect(domain, html=html, title=title, meta_description=meta_description))
    except Exception as exc:
        log.error("AI classify failed for %s: %s", domain, exc)
        raise self.retry(exc=exc)

    with _get_sync_session() as session:
        values = {
            "ai_website_type": result.website_type,
            "ai_confidence": result.confidence,
            "ai_classified_at": datetime.now(timezone.utc),
        }
        if result.is_company:
            values.update({
                "name": result.name,
                "description": result.description,
                "industry": result.industry,
                "country": result.country,
                "size_estimate": result.size_estimate,
                "founded_year": result.founded_year,
                "crawl_status": "verified",
            })
            if result.linkedin_url:
                values["social_links"] = {"linkedin": result.linkedin_url}
        else:
            values["crawl_status"] = "skipped"

        session.execute(
            update(Company).where(Company.domain == domain).values(**values)
        )
        session.commit()

    # If it's a company, trigger email enrichment
    if result.is_company and result.confidence >= 0.7:
        task_enrich.apply_async(args=[domain], queue="enrich")

    return {
        "domain": domain,
        "is_company": result.is_company,
        "confidence": result.confidence,
        "type": result.website_type,
        "name": result.name,
    }


# ── Task 4: Enrich with Hunter.io ─────────────────────────────────────────────

@shared_task(
    name="workers.tasks.task_enrich",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def task_enrich(self, domain: str) -> dict:
    """Find email contacts via Hunter.io and save to DB."""
    from db.models import Company, CompanyEmail
    import httpx

    if not settings.hunter_api_key:
        log.debug("Hunter API key not set, skipping enrich for %s", domain)
        return {"skipped": True}

    log.info("Enriching: %s", domain)

    try:
        resp = httpx.get(
            "https://api.hunter.io/v2/domain-search",
            params={
                "domain": domain,
                "api_key": settings.hunter_api_key,
                "limit": 10,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
    except Exception as exc:
        raise self.retry(exc=exc)

    emails_added = 0
    with _get_sync_session() as session:
        company_id = session.execute(
            select(Company.id).where(Company.domain == domain)
        ).scalar_one_or_none()

        if not company_id:
            return {"domain": domain, "error": "company not found in db"}

        for contact in data.get("emails", []):
            stmt = pg_insert(CompanyEmail).values(
                company_id=company_id,
                email=contact.get("value", ""),
                first_name=contact.get("first_name"),
                last_name=contact.get("last_name"),
                position=contact.get("position"),
                confidence=contact.get("confidence"),
                source="hunter",
            ).on_conflict_do_nothing()
            result = session.execute(stmt)
            if result.rowcount:
                emails_added += 1

        session.commit()

    log.info("Enriched %s: added %d emails", domain, emails_added)
    return {"domain": domain, "emails_added": emails_added}


# ── Task 5: Dispatch pending domains ──────────────────────────────────────────

@shared_task(name="workers.tasks.task_dispatch_pending_domains")
def task_dispatch_pending_domains(batch_size: int = 500) -> dict:
    """
    Periodic task: pick pending domains from DB and dispatch crawl tasks.
    Called every 30 minutes via Celery Beat.
    """
    from db.models import Company

    with _get_sync_session() as session:
        rows = session.execute(
            select(Company.domain)
            .where(Company.crawl_status == "pending")
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).scalars().all()

        if not rows:
            return {"dispatched": 0}

        # Mark as 'crawling' to prevent double-dispatch
        session.execute(
            update(Company)
            .where(Company.domain.in_(rows))
            .values(crawl_status="crawling")
        )
        session.commit()

    for domain in rows:
        task_crawl_domain.apply_async(args=[domain], queue="crawl")

    log.info("Dispatched %d domains for crawling", len(rows))
    return {"dispatched": len(rows)}


# ── Task: Bulk ingest from Common Crawl ──────────────────────────────────────

@shared_task(name="workers.tasks.task_collect_common_crawl")
def task_collect_common_crawl(
    tlds: list[str] = None,
    limit_per_tld: int = 50_000,
) -> dict:
    """Collect domains from Common Crawl CDX API and insert into DB."""
    from db.models import Company

    tlds = tlds or ["com", "io", "co", "net"]
    log.info("Collecting from Common Crawl: tlds=%s", tlds)

    collector = CommonCrawlCollector()

    inserted = 0

    async def _collect():
        nonlocal inserted
        with _get_sync_session() as session:
            async for domain in collector.iter_domains_multi_tld(
                tlds=tlds, limit_per_tld=limit_per_tld
            ):
                stmt = pg_insert(Company).values(
                    domain=domain,
                    source="common_crawl",
                    crawl_status="pending",
                ).on_conflict_do_nothing(index_elements=["domain"])
                result = session.execute(stmt)
                if result.rowcount:
                    inserted += 1

                if inserted % 1000 == 0:
                    session.commit()
                    log.info("Common Crawl: %d domains inserted so far", inserted)

            session.commit()

    _run_async(_collect())
    log.info("Common Crawl total inserted: %d", inserted)
    return {"inserted": inserted}
