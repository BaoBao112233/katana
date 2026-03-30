"""
collectors/common_crawl.py
──────────────────────────────────────────────────────
Read domain list from Common Crawl CDXJ index (S3/HTTP).
Common Crawl is FREE and has ~80B pages indexed monthly.

Strategy:
  - Query the CDX Server API to get URLs for a pattern
  - Or download the cc-index.paths.gz for bulk processing
  - For MVP: use CDX API to sample domains

Common Crawl CDX API:
  https://index.commoncrawl.org/CC-MAIN-2024-51-index?url=*.com&output=json

Usage:
    collector = CommonCrawlCollector()
    async for domain in collector.iter_domains(tld="com", limit=100_000):
        print(domain)
"""

import asyncio
import gzip
import io
import json
import logging
from typing import AsyncIterator, Optional

import httpx

from config.settings import settings
from utils.domain_utils import normalize_domain, deduplicate_domains

log = logging.getLogger(__name__)

CDX_API_BASE = "https://index.commoncrawl.org"
CC_S3_BASE = "https://data.commoncrawl.org"


class CommonCrawlCollector:
    """
    Collect unique domains from Common Crawl CDX index.

    MVP mode: query CDX API for wildcard patterns
    Production mode: download full cc-index files from S3
    """

    def __init__(self, crawl_index: Optional[str] = None):
        self.crawl_index = crawl_index or settings.common_crawl_index

    # ── MVP: CDX API ───────────────────────────────────────────────────────────

    async def iter_domains(
        self,
        tld: str = "com",
        limit: int = 100_000,
        filter_mime: Optional[str] = "text/html",
    ) -> AsyncIterator[str]:
        """
        Iterate unique domains from CDX API by TLD wildcard.
        Good for sampling up to ~1M domains.

        :param tld: top-level domain to sample (com, io, co, net, ...)
        :param limit: max domains to yield
        :param filter_mime: filter by MIME type
        """
        url = f"{CDX_API_BASE}/{self.crawl_index}-index"
        params = {
            "url": f"*.{tld}",
            "output": "json",
            "fl": "url",
            "limit": min(limit, 100_000),
        }
        if filter_mime:
            params["filter"] = f"mime:{filter_mime}"

        seen: set[str] = set()
        count = 0

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            # CDX API streams JSON lines
            async with client.stream("GET", url, params=params, timeout=120) as resp:
                if resp.status_code != 200:
                    log.error("CDX API returned %d for %s", resp.status_code, url)
                    return

                async for raw_line in resp.aiter_lines():
                    if count >= limit:
                        break
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        obj = json.loads(raw_line)
                        domain = normalize_domain(obj.get("url", ""))
                        if domain and domain not in seen:
                            seen.add(domain)
                            yield domain
                            count += 1
                    except json.JSONDecodeError:
                        continue

        log.info("CommonCrawlCollector yielded %d domains (tld=.%s)", count, tld)

    async def iter_domains_multi_tld(
        self,
        tlds: list[str] = ("com", "io", "co", "net", "org"),
        limit_per_tld: int = 50_000,
    ) -> AsyncIterator[str]:
        """Iterate domains across multiple TLDs."""
        seen: set[str] = set()
        for tld in tlds:
            async for domain in self.iter_domains(tld=tld, limit=limit_per_tld):
                if domain not in seen:
                    seen.add(domain)
                    yield domain

    # ── Production: Download index files from S3 ───────────────────────────────

    async def download_index_file(
        self,
        segment_path: str,
        output_callback,
    ) -> int:
        """
        Download a single cc-index segment file (gzipped CDXJ).
        Calls output_callback(domain: str) for each unique domain found.

        segment_path: relative path from cc-index.paths.gz
            e.g. "cc-index/collections/CC-MAIN-2024-51/indexes/cdx-00000.gz"

        Returns: count of domains extracted.
        """
        url = f"{CC_S3_BASE}/{segment_path}"
        count = 0
        seen: set[str] = set()

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.get(url)
                resp.raise_for_status()

            # Decompress in memory
            buf = io.BytesIO(resp.content)
            with gzip.GzipFile(fileobj=buf) as gz:
                for raw_line in gz:
                    try:
                        # CDXJ format: "domain.tld) path date json"
                        parts = raw_line.decode("utf-8", errors="ignore").split(" ", 3)
                        if len(parts) >= 4:
                            obj = json.loads(parts[3])
                            domain = normalize_domain(obj.get("url", ""))
                            if domain and domain not in seen:
                                seen.add(domain)
                                await output_callback(domain)
                                count += 1
                    except Exception:
                        continue
        except Exception as e:
            log.error("Failed to download CC segment %s: %s", segment_path, e)

        return count

    async def get_index_paths(self) -> list[str]:
        """Fetch list of all CDX index segment paths for the current crawl."""
        url = f"{CC_S3_BASE}/crawl-data/{self.crawl_index}/cc-index.paths.gz"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            buf = io.BytesIO(resp.content)
            with gzip.GzipFile(fileobj=buf) as gz:
                paths = [line.decode().strip() for line in gz if line.strip()]
            log.info("Found %d CC index segment paths", len(paths))
            return paths
        except Exception as e:
            log.error("Failed to fetch CC index paths: %s", e)
            return []
