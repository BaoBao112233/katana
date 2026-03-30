"""
collectors/google_maps.py
──────────────────────────────────────────────────────
Scrape Google Maps for business listings by category + location.
Returns domain + basic company info for each result.

Strategy:
  1. Use Google Maps Places API (preferred, structured)
  2. Fallback: scrape search.google.com (rate-limited)

Requirements:
  - GOOGLE_MAPS_API_KEY in .env for Places API mode

Usage:
    collector = GoogleMapsCollector()
    async for company in collector.collect(query="software company", location="Ho Chi Minh City"):
        print(company)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import httpx

from config.settings import settings
from utils.domain_utils import normalize_domain

log = logging.getLogger(__name__)

PLACES_API_BASE = "https://maps.googleapis.com/maps/api/place"


@dataclass
class RawCompany:
    """Minimal company record from a collector."""
    name: str
    domain: Optional[str] = None
    url: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    country: Optional[str] = None
    category: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    source: str = "google_maps"
    external_id: Optional[str] = None          # place_id, etc.
    raw: dict = field(default_factory=dict)


class GoogleMapsCollector:
    """
    Collect businesses from Google Maps Places API.

    Paginates automatically using next_page_token.
    Yields RawCompany objects one by one.

    Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.google_maps_api_key
        if not self.api_key:
            log.warning(
                "GOOGLE_MAPS_API_KEY not set. GoogleMapsCollector will not work."
            )

    async def collect(
        self,
        query: str,
        location: str = "",
        radius_m: int = 50_000,
        max_results: int = 200,
    ) -> AsyncIterator[RawCompany]:
        """
        Async generator yielding RawCompany objects.

        :param query: search term e.g. "software company"
        :param location: human-readable location e.g. "Ho Chi Minh City, Vietnam"
        :param radius_m: search radius in meters
        :param max_results: stop after this many results
        """
        if not self.api_key:
            log.error("No Google Maps API key configured.")
            return

        # First, resolve location name to lat/lng
        lat, lng = await self._geocode(location) if location else (None, None)

        params = {
            "query": query,
            "key": self.api_key,
            "fields": "name,website,formatted_phone_number,formatted_address,geometry,place_id,types",
        }
        if lat and lng:
            params["location"] = f"{lat},{lng}"
            params["radius"] = str(radius_m)

        count = 0
        next_page_token: Optional[str] = None

        async with httpx.AsyncClient(timeout=30) as client:
            while count < max_results:
                if next_page_token:
                    # Google requires a short delay before using page token
                    await asyncio.sleep(2)
                    req_params = {"pagetoken": next_page_token, "key": self.api_key}
                else:
                    req_params = params

                resp = await client.get(
                    f"{PLACES_API_BASE}/textsearch/json",
                    params=req_params,
                )
                resp.raise_for_status()
                data = resp.json()

                if data.get("status") not in ("OK", "ZERO_RESULTS"):
                    log.error("Places API error: %s — %s", data.get("status"), data.get("error_message"))
                    break

                for place in data.get("results", []):
                    if count >= max_results:
                        break
                    yield self._parse_place(place)
                    count += 1

                next_page_token = data.get("next_page_token")
                if not next_page_token:
                    break

        log.info("GoogleMapsCollector yielded %d companies for query=%r", count, query)

    async def collect_batch_queries(
        self,
        queries: list[str],
        location: str = "",
        max_per_query: int = 200,
    ) -> AsyncIterator[RawCompany]:
        """Run multiple queries sequentially and deduplicate by domain."""
        seen_domains: set[str] = set()
        for q in queries:
            async for company in self.collect(q, location=location, max_results=max_per_query):
                key = company.domain or company.name
                if key not in seen_domains:
                    seen_domains.add(key)
                    yield company
                await asyncio.sleep(0)  # yield control

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _parse_place(self, place: dict) -> RawCompany:
        website = place.get("website", "")
        domain = normalize_domain(website) if website else None

        geo = place.get("geometry", {}).get("location", {})
        types = place.get("types", [])

        return RawCompany(
            name=place.get("name", ""),
            domain=domain,
            url=website or None,
            phone=place.get("formatted_phone_number"),
            address=place.get("formatted_address"),
            category=types[0] if types else None,
            lat=geo.get("lat"),
            lng=geo.get("lng"),
            external_id=place.get("place_id"),
            source="google_maps",
            raw=place,
        )

    async def _geocode(self, location: str) -> tuple[Optional[float], Optional[float]]:
        """Convert location name to lat/lng via Geocoding API."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://maps.googleapis.com/maps/api/geocode/json",
                    params={"address": location, "key": self.api_key},
                )
                data = resp.json()
                if data.get("results"):
                    loc = data["results"][0]["geometry"]["location"]
                    return loc["lat"], loc["lng"]
        except Exception as e:
            log.warning("Geocoding failed for %r: %s", location, e)
        return None, None
