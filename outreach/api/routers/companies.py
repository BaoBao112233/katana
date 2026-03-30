"""
api/routers/companies.py
──────────────────────────────────────────────────────
CRUD + bulk import endpoints for the companies resource.

Endpoints:
  GET    /companies                — list with filters + pagination
  GET    /companies/{domain}       — get by domain
  POST   /companies/import         — bulk import list of domains
  DELETE /companies/{domain}       — remove (admin only)
  GET    /companies/{domain}/emails — get email contacts
"""

import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Company, CompanyEmail
from utils.domain_utils import normalize_domain

router = APIRouter()


# ── Response schemas ───────────────────────────────────────────────────────────

class CompanyOut(BaseModel):
    id: uuid.UUID
    domain: str
    name: Optional[str]
    description: Optional[str]
    industry: Optional[str]
    country: Optional[str]
    size_estimate: Optional[str]
    founded_year: Optional[int]
    url: Optional[str]
    social_links: Optional[dict]
    ai_website_type: Optional[str]
    ai_confidence: Optional[float]
    crawl_status: str
    source: Optional[str]

    class Config:
        from_attributes = True


class CompanyListOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[CompanyOut]


class EmailOut(BaseModel):
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    position: Optional[str]
    confidence: Optional[int]
    source: Optional[str]

    class Config:
        from_attributes = True


class ImportRequest(BaseModel):
    domains: list[str] = Field(..., min_length=1, max_length=10_000)
    source: str = "manual"


class ImportResult(BaseModel):
    submitted: int
    skipped: int
    message: str


# ── Dependency ─────────────────────────────────────────────────────────────────

async def get_db_session() -> AsyncSession:
    from api.main import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        yield session


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("", response_model=CompanyListOut)
async def list_companies(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    industry: Optional[str] = None,
    country: Optional[str] = None,
    size: Optional[str] = None,
    crawl_status: Optional[str] = None,
    min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0),
    db: AsyncSession = Depends(get_db_session),
):
    """
    List companies with optional filters.

    Filters: industry, country (ISO 3166-1 alpha-2), size, crawl_status, min_confidence
    """
    q = select(Company)

    if industry:
        q = q.where(Company.industry.ilike(f"%{industry}%"))
    if country:
        q = q.where(Company.country == country.upper())
    if size:
        q = q.where(Company.size_estimate == size)
    if crawl_status:
        q = q.where(Company.crawl_status == crawl_status)
    if min_confidence is not None:
        q = q.where(Company.ai_confidence >= min_confidence)

    # Count
    count_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = count_result.scalar_one()

    # Paginate
    q = q.offset((page - 1) * page_size).limit(page_size)
    q = q.order_by(Company.created_at.desc())

    result = await db.execute(q)
    items = result.scalars().all()

    return CompanyListOut(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )


@router.get("/{domain}", response_model=CompanyOut)
async def get_company(
    domain: str,
    db: AsyncSession = Depends(get_db_session),
):
    """Get a company by domain name."""
    clean = normalize_domain(domain)
    if not clean:
        raise HTTPException(status_code=400, detail="Invalid domain")

    result = await db.execute(select(Company).where(Company.domain == clean))
    company = result.scalar_one_or_none()

    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    return company


@router.get("/{domain}/emails", response_model=list[EmailOut])
async def get_company_emails(
    domain: str,
    db: AsyncSession = Depends(get_db_session),
):
    """Get email contacts for a company."""
    clean = normalize_domain(domain)
    result = await db.execute(select(Company.id).where(Company.domain == clean))
    company_id = result.scalar_one_or_none()

    if not company_id:
        raise HTTPException(status_code=404, detail="Company not found")

    emails = await db.execute(
        select(CompanyEmail).where(CompanyEmail.company_id == company_id)
    )
    return emails.scalars().all()


@router.post(
    "/import",
    response_model=ImportResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_domains(
    body: ImportRequest,
    db: AsyncSession = Depends(get_db_session),
):
    """
    Bulk import a list of domains. Each will be queued for crawling.
    Accepts up to 10,000 domains per request.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from db.models import Company
    from workers.tasks import task_crawl_domain

    submitted = 0
    skipped = 0

    for raw_domain in body.domains:
        domain = normalize_domain(raw_domain)
        if not domain:
            skipped += 1
            continue

        stmt = pg_insert(Company).values(
            domain=domain,
            source=body.source,
            crawl_status="pending",
        ).on_conflict_do_nothing(index_elements=["domain"])

        result = await db.execute(stmt)
        if result.rowcount:
            submitted += 1
        else:
            skipped += 1

    await db.commit()

    # Dispatch crawl tasks
    for raw_domain in body.domains:
        domain = normalize_domain(raw_domain)
        if domain:
            task_crawl_domain.apply_async(args=[domain], queue="crawl")

    return ImportResult(
        submitted=submitted,
        skipped=skipped,
        message=f"Queued {submitted} domains for crawling",
    )


@router.delete("/{domain}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_company(
    domain: str,
    db: AsyncSession = Depends(get_db_session),
):
    """Remove a company from the database."""
    from sqlalchemy import delete

    clean = normalize_domain(domain)
    result = await db.execute(
        select(Company).where(Company.domain == clean)
    )
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    await db.delete(company)
    await db.commit()
