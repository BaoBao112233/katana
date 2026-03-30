"""
db/models.py
──────────────────────────────────────────────────────
SQLAlchemy 2.0 async ORM models for the company database.
Database: PostgreSQL (via asyncpg)

Tables:
  - companies     — core company records
  - company_emails — email contacts per company
  - crawl_jobs    — queue + status of crawl operations
  - api_keys      — SaaS user API key management

Run migrations with Alembic:
  alembic upgrade head
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── Company ────────────────────────────────────────────────────────────────────

class Company(Base):
    """Core company record. One row per domain."""

    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("domain", name="uq_company_domain"),
        Index("ix_company_name_lower", func.lower("name")),
        Index("ix_company_industry", "industry"),
        Index("ix_company_country", "country"),
        Index("ix_company_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(500))
    description: Mapped[Optional[str]] = mapped_column(Text)
    industry: Mapped[Optional[str]] = mapped_column(String(100))
    country: Mapped[Optional[str]] = mapped_column(String(2))   # ISO 3166-1 alpha-2
    size_estimate: Mapped[Optional[str]] = mapped_column(String(20))
    founded_year: Mapped[Optional[int]] = mapped_column(Integer)
    employee_count: Mapped[Optional[int]] = mapped_column(Integer)

    # Website
    url: Mapped[Optional[str]] = mapped_column(String(2048))
    title: Mapped[Optional[str]] = mapped_column(String(500))
    meta_description: Mapped[Optional[str]] = mapped_column(Text)

    # Social links (JSON object: {platform: url})
    social_links: Mapped[Optional[dict]] = mapped_column(JSONB)

    # AI classification
    ai_website_type: Mapped[Optional[str]] = mapped_column(String(50))   # company/blog/etc
    ai_confidence: Mapped[Optional[float]] = mapped_column(Float)
    ai_classified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Crawl status
    crawl_status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True
    )  # pending | crawling | done | failed | skipped
    crawled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    crawl_error: Mapped[Optional[str]] = mapped_column(Text)
    http_status: Mapped[Optional[int]] = mapped_column(Integer)

    # Data quality
    heuristic_score: Mapped[Optional[int]] = mapped_column(Integer)  # 0-100
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # Source
    source: Mapped[Optional[str]] = mapped_column(String(50))  # google_maps / common_crawl / manual
    external_id: Mapped[Optional[str]] = mapped_column(String(200))  # place_id etc.

    # Extra metadata blob
    metadata: Mapped[Optional[dict]] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    emails: Mapped[list["CompanyEmail"]] = relationship(
        "CompanyEmail", back_populates="company", cascade="all, delete-orphan"
    )
    crawl_jobs: Mapped[list["CrawlJob"]] = relationship(
        "CrawlJob", back_populates="company", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Company {self.domain!r} ({self.name!r})>"


# ── Company Emails ─────────────────────────────────────────────────────────────

class CompanyEmail(Base):
    __tablename__ = "company_emails"
    __table_args__ = (
        UniqueConstraint("company_id", "email", name="uq_company_email"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    first_name: Mapped[Optional[str]] = mapped_column(String(100))
    last_name: Mapped[Optional[str]] = mapped_column(String(100))
    position: Mapped[Optional[str]] = mapped_column(String(200))
    confidence: Mapped[Optional[int]] = mapped_column(Integer)  # 0-100 (Hunter score)
    source: Mapped[Optional[str]] = mapped_column(String(50))   # hunter / crawler / apollo

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    company: Mapped["Company"] = relationship("Company", back_populates="emails")


# ── Crawl Jobs ─────────────────────────────────────────────────────────────────

class CrawlJob(Base):
    """Tracks the status of each crawl attempt."""

    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # queued | running | done | failed

    celery_task_id: Mapped[Optional[str]] = mapped_column(String(200))
    worker_host: Mapped[Optional[str]] = mapped_column(String(200))
    proxy_used: Mapped[Optional[str]] = mapped_column(String(500))

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    company: Mapped["Company"] = relationship("Company", back_populates="crawl_jobs")


# ── API Keys (SaaS) ────────────────────────────────────────────────────────────

class ApiKey(Base):
    """SaaS user API key management."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200))
    owner_email: Mapped[str] = mapped_column(String(254), index=True)

    tier: Mapped[str] = mapped_column(String(20), default="free")
    # free | starter | pro | enterprise

    requests_per_day: Mapped[int] = mapped_column(Integer, default=100)
    requests_today: Mapped[int] = mapped_column(Integer, default=0)
    requests_total: Mapped[int] = mapped_column(BigInteger, default=0)
    last_reset_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
