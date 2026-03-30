"""
collectors/domain_generator.py
──────────────────────────────────────────────────────
Generate domain candidates from company names + verify via DNS.
Also integrates with Google Search to find official domains.

Usage:
    gen = DomainGenerator()
    domain = await gen.find_domain("Stripe Inc")
    # -> "stripe.com"
"""

import asyncio
import logging
from typing import Optional

import httpx

from config.settings import settings
from utils.domain_utils import (
    find_live_domain,
    generate_domain_candidates,
    normalize_domain,
)

log = logging.getLogger(__name__)

GOOGLE_SEARCH_API = "https://www.googleapis.com/customsearch/v1"


class DomainGenerator:
    """
    Multi-strategy domain finder for companies without known websites.

    Priority:
      1. Google Custom Search API (most accurate)
      2. DNS inference from generated candidates
    """

    def __init__(
        self,
        google_api_key: Optional[str] = None,
        google_cx: Optional[str] = None,
    ):
        self.google_api_key = google_api_key or settings.google_api_key
        self.google_cx = google_cx or settings.google_cx

    async def find_domain(self, company_name: str, country: str = "") -> Optional[str]:
        """
        Find official domain for a company.
        Returns normalized domain or None.
        """
        # Strategy 1: Google Search
        if self.google_api_key and self.google_cx:
            domain = await self._google_search_domain(company_name, country)
            if domain:
                log.debug("Found via Google: %s -> %s", company_name, domain)
                return domain

        # Strategy 2: DNS inference
        domain = await find_live_domain(company_name)
        if domain:
            log.debug("Found via DNS: %s -> %s", company_name, domain)
            return domain

        log.debug("No domain found for: %s", company_name)
        return None

    async def find_domains_batch(
        self,
        company_names: list[str],
        country: str = "",
        concurrency: int = 5,
    ) -> dict[str, Optional[str]]:
        """
        Find domains for multiple companies concurrently.
        Returns dict: {company_name: domain_or_None}
        """
        semaphore = asyncio.Semaphore(concurrency)

        async def _find(name: str) -> tuple[str, Optional[str]]:
            async with semaphore:
                return name, await self.find_domain(name, country)

        tasks = [_find(name) for name in company_names]
        results = await asyncio.gather(*tasks)
        return dict(results)

    # ── Private ────────────────────────────────────────────────────────────────

    async def _google_search_domain(
        self,
        company_name: str,
        country: str = "",
    ) -> Optional[str]:
        """Query Google Custom Search for company official website."""
        query = f'"{company_name}" official website'
        if country:
            query += f" {country}"

        params = {
            "key": self.google_api_key,
            "cx": self.google_cx,
            "q": query,
            "num": 3,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(GOOGLE_SEARCH_API, params=params)
                if resp.status_code == 429:
                    log.warning("Google Search API rate limited")
                    return None
                resp.raise_for_status()
                data = resp.json()

            for item in data.get("items", []):
                link = item.get("link", "")
                domain = normalize_domain(link)
                if domain:
                    return domain

        except Exception as e:
            log.debug("Google search failed for %r: %s", company_name, e)

        return None
