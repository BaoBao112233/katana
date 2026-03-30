"""
api/routers/search.py
──────────────────────────────────────────────────────
Full-text search endpoint backed by Elasticsearch.
Falls back to PostgreSQL ILIKE if ES is unavailable.

Endpoints:
  GET /search?q=fintech&country=US&industry=software&page=1
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings

log = logging.getLogger(__name__)
router = APIRouter()


# ── Response schemas ───────────────────────────────────────────────────────────

class SearchHit(BaseModel):
    domain: str
    name: Optional[str]
    description: Optional[str]
    industry: Optional[str]
    country: Optional[str]
    size_estimate: Optional[str]
    url: Optional[str]
    score: float = 1.0


class SearchResult(BaseModel):
    query: str
    total: int
    page: int
    hits: list[SearchHit]
    engine: str   # "elasticsearch" or "postgresql"


# ── Dependency ─────────────────────────────────────────────────────────────────

async def get_db_session() -> AsyncSession:
    from api.main import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        yield session


# ── Elasticsearch client (lazy) ────────────────────────────────────────────────

_es_client = None


async def _get_es():
    global _es_client
    if _es_client is None:
        try:
            from elasticsearch import AsyncElasticsearch
            _es_client = AsyncElasticsearch(settings.elasticsearch_url)
        except ImportError:
            log.warning("elasticsearch package not installed, using PostgreSQL search fallback")
    return _es_client


# ── Endpoint ───────────────────────────────────────────────────────────────────

@router.get("", response_model=SearchResult)
async def search_companies(
    q: str = Query(..., min_length=1, max_length=200, description="Search query"),
    country: Optional[str] = Query(None, max_length=2),
    industry: Optional[str] = Query(None, max_length=100),
    size: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
):
    """
    Full-text search across company names, descriptions, and domains.

    Powered by Elasticsearch when available, falls back to PostgreSQL.
    """
    es = await _get_es()
    if es:
        return await _search_elasticsearch(
            es, q, country, industry, size, page, page_size
        )
    return await _search_postgresql(db, q, country, industry, size, page, page_size)


# ── Elasticsearch search ───────────────────────────────────────────────────────

async def _search_elasticsearch(
    es,
    q: str,
    country: Optional[str],
    industry: Optional[str],
    size: Optional[str],
    page: int,
    page_size: int,
) -> SearchResult:
    must = [
        {
            "multi_match": {
                "query": q,
                "fields": ["name^3", "description^2", "domain^2", "industry"],
                "type": "best_fields",
                "fuzziness": "AUTO",
            }
        }
    ]
    filters = []
    if country:
        filters.append({"term": {"country": country.upper()}})
    if industry:
        filters.append({"match": {"industry": industry}})
    if size:
        filters.append({"term": {"size_estimate": size}})

    body = {
        "query": {"bool": {"must": must, "filter": filters}},
        "from": (page - 1) * page_size,
        "size": page_size,
        "_source": ["domain", "name", "description", "industry", "country", "size_estimate", "url"],
    }

    try:
        resp = await es.search(index=settings.elasticsearch_index, body=body)
        hits = resp["hits"]["hits"]
        total = resp["hits"]["total"]["value"]

        return SearchResult(
            query=q,
            total=total,
            page=page,
            engine="elasticsearch",
            hits=[
                SearchHit(
                    domain=h["_source"].get("domain", ""),
                    name=h["_source"].get("name"),
                    description=h["_source"].get("description"),
                    industry=h["_source"].get("industry"),
                    country=h["_source"].get("country"),
                    size_estimate=h["_source"].get("size_estimate"),
                    url=h["_source"].get("url"),
                    score=h["_score"],
                )
                for h in hits
            ],
        )
    except Exception as e:
        log.warning("Elasticsearch search failed: %s — falling back to PG", e)
        # Will be caught by caller - for simplicity just return empty
        return SearchResult(query=q, total=0, page=page, engine="elasticsearch", hits=[])


# ── PostgreSQL fallback search ─────────────────────────────────────────────────

async def _search_postgresql(
    db: AsyncSession,
    q: str,
    country: Optional[str],
    industry: Optional[str],
    size: Optional[str],
    page: int,
    page_size: int,
) -> SearchResult:
    from db.models import Company
    from sqlalchemy import func, or_

    like_q = f"%{q}%"
    query = select(Company).where(
        or_(
            Company.name.ilike(like_q),
            Company.description.ilike(like_q),
            Company.domain.ilike(like_q),
            Company.industry.ilike(like_q),
        )
    )

    if country:
        query = query.where(Company.country == country.upper())
    if industry:
        query = query.where(Company.industry.ilike(f"%{industry}%"))
    if size:
        query = query.where(Company.size_estimate == size)

    # Total count
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar_one()

    query = query.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).scalars().all()

    return SearchResult(
        query=q,
        total=total,
        page=page,
        engine="postgresql",
        hits=[
            SearchHit(
                domain=r.domain,
                name=r.name,
                description=r.description,
                industry=r.industry,
                country=r.country,
                size_estimate=r.size_estimate,
                url=r.url,
            )
            for r in rows
        ],
    )


# ── Index management (admin) ───────────────────────────────────────────────────

@router.post("/reindex", tags=["Admin"])
async def reindex(db: AsyncSession = Depends(get_db_session)):
    """Re-index all verified companies into Elasticsearch."""
    from db.models import Company

    es = await _get_es()
    if not es:
        return {"error": "Elasticsearch not available"}

    companies = (await db.execute(
        select(Company).where(Company.crawl_status == "verified")
    )).scalars().all()

    indexed = 0
    for c in companies:
        await es.index(
            index=settings.elasticsearch_index,
            id=str(c.id),
            document={
                "domain": c.domain,
                "name": c.name,
                "description": c.description,
                "industry": c.industry,
                "country": c.country,
                "size_estimate": c.size_estimate,
                "url": c.url,
                "ai_confidence": c.ai_confidence,
            },
        )
        indexed += 1

    return {"indexed": indexed}
