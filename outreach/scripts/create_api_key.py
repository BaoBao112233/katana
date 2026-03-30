#!/usr/bin/env python3
"""
scripts/create_api_key.py
──────────────────────────────────────────────────────
Create a new SaaS API key and save to the database.

Usage:
    python scripts/create_api_key.py --name "My App" --email me@example.com --tier pro
"""

import argparse
import asyncio
import hashlib
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings


async def create_key(name: str, email: str, tier: str) -> str:
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from db.models import ApiKey, Base

    raw_key = f"cdb_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    limits = {"free": 100, "starter": 1000, "pro": 10_000, "enterprise": 100_000}

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        api_key = ApiKey(
            key_hash=key_hash,
            name=name,
            owner_email=email,
            tier=tier,
            requests_per_day=limits.get(tier, 100),
        )
        session.add(api_key)
        await session.commit()

    await engine.dispose()
    return raw_key


def main():
    parser = argparse.ArgumentParser(description="Create a SaaS API key")
    parser.add_argument("--name", required=True, help="Key name / description")
    parser.add_argument("--email", required=True, help="Owner email")
    parser.add_argument("--tier", default="free", choices=["free", "starter", "pro", "enterprise"])
    args = parser.parse_args()

    key = asyncio.run(create_key(args.name, args.email, args.tier))
    print(f"\n✓ API key created successfully!\n")
    print(f"  Key:   {key}")
    print(f"  Name:  {args.name}")
    print(f"  Email: {args.email}")
    print(f"  Tier:  {args.tier}")
    print(f"\nAdd to requests: X-API-Key: {key}\n")


if __name__ == "__main__":
    main()
