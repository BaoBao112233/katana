"""
api/main.py
──────────────────────────────────────────────────────
FastAPI SaaS application — Company Database API.

Endpoints:
  GET  /companies         — list + filter companies
  GET  /companies/{id}    — company detail
  GET  /search            — full-text search (Elasticsearch)
  POST /companies/import  — bulk domain import
  GET  /stats             — database statistics

Run:
  uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

Auth:
  Pass API key in header: X-API-Key: your_key
"""

import hashlib
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from api.routers import companies, search
from config.settings import settings
from db.models import ApiKey, Base

log = logging.getLogger(__name__)

# ── DB setup ───────────────────────────────────────────────────────────────────

engine = create_async_engine(
    settings.database_url,
    pool_size=20,
    max_overflow=40,
    pool_pre_ping=True,
    echo=settings.debug,
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup (use Alembic for production migrations)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Database tables ready")
    yield
    await engine.dispose()
    log.info("Database connection pool closed")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Company Database API",
    description="SaaS API for searching and exploring company data. Like Crunchbase, but yours.",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.api_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Dependency: DB session ─────────────────────────────────────────────────────

async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


# ── Dependency: API key auth ───────────────────────────────────────────────────

async def require_api_key(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ApiKey:
    """Validate X-API-Key header."""
    key = request.headers.get("X-API-Key", "")
    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )

    key_hash = hashlib.sha256(key.encode()).hexdigest()

    from sqlalchemy import select
    row = await db.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active == True)
    )
    api_key = row.scalar_one_or_none()

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or inactive API key",
        )

    # Rate limit check
    from datetime import date
    today = date.today().isoformat()
    if str(api_key.last_reset_date)[:10] != today:
        api_key.requests_today = 0
        api_key.last_reset_date = date.today()

    if api_key.requests_today >= api_key.requests_per_day:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily limit of {api_key.requests_per_day} requests reached",
        )

    api_key.requests_today += 1
    api_key.requests_total += 1
    await db.commit()

    return api_key


# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(
    companies.router,
    prefix="/companies",
    tags=["Companies"],
    dependencies=[Depends(require_api_key)],
)
app.include_router(
    search.router,
    prefix="/search",
    tags=["Search"],
    dependencies=[Depends(require_api_key)],
)


# ── System endpoints ───────────────────────────────────────────────────────────

@app.get("/", tags=["System"])
async def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "status": "ok",
    }


@app.get("/health", tags=["System"])
async def health(db: AsyncSession = Depends(get_db)):
    """Health check endpoint for load balancer / k8s probe."""
    try:
        await db.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "error",
    }


@app.get("/stats", tags=["System"])
async def stats(db: AsyncSession = Depends(get_db)):
    """Database statistics — no auth required."""
    from sqlalchemy import func, select
    from db.models import Company

    result = await db.execute(
        select(
            func.count(Company.id).label("total"),
            func.count(Company.id).filter(Company.crawl_status == "verified").label("verified"),
            func.count(Company.id).filter(Company.crawl_status == "pending").label("pending"),
            func.count(Company.id).filter(Company.industry.isnot(None)).label("with_industry"),
        )
    )
    row = result.one()
    return {
        "total_companies": row.total,
        "verified_companies": row.verified,
        "pending_crawl": row.pending,
        "with_industry_data": row.with_industry,
    }


# ── Exception handlers ─────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
