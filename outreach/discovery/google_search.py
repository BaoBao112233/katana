"""
discovery/google_search.py
Tìm công ty bằng Google Custom Search JSON API.
https://developers.google.com/custom-search/v1/introduction
"""

import time
import logging
import re
from urllib.parse import urlparse
from typing import Generator

import requests

log = logging.getLogger(__name__)


class GoogleSearchDiscovery:
    """Tìm website công ty qua Google Custom Search API."""

    BASE_URL = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, api_key: str, cx: str, country: str = ""):
        self.api_key = api_key
        self.cx = cx
        self.country = country

    def _search_page(self, query: str, start: int = 1) -> list[dict]:
        """Gọi API một trang (10 kết quả)."""
        params: dict = {
            "key": self.api_key,
            "cx": self.cx,
            "q": query,
            "start": start,
            "num": 10,
        }
        if self.country:
            params["gl"] = self.country

        try:
            resp = requests.get(self.BASE_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            return data.get("items", [])
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 429:
                log.warning("Google API rate-limited. Chờ 60 giây...")
                time.sleep(60)
            else:
                log.error("Google API lỗi HTTP %s: %s", resp.status_code, e)
            return []
        except Exception as e:
            log.error("Google Search lỗi: %s", e)
            return []

    def discover(self, queries: list[str], pages_per_query: int = 3) -> Generator[dict, None, None]:
        """
        Yield từng công ty tìm được.
        Mỗi item: {"name": ..., "url": ..., "domain": ..., "snippet": ...}
        """
        seen_domains: set[str] = set()

        for query in queries:
            log.info("Google Search: '%s' (%d trang)", query, pages_per_query)
            for page in range(pages_per_query):
                start = page * 10 + 1
                items = self._search_page(query, start=start)
                if not items:
                    break

                for item in items:
                    url = item.get("link", "")
                    domain = _extract_domain(url)
                    if not domain or domain in seen_domains:
                        continue
                    seen_domains.add(domain)

                    company = {
                        "name": item.get("title", domain),
                        "url": url,
                        "domain": domain,
                        "snippet": item.get("snippet", ""),
                        "source": "google",
                    }
                    log.debug("Tìm thấy: %s (%s)", company["name"], domain)
                    yield company

                # Tránh bị rate-limit Google
                time.sleep(1.5)


def _extract_domain(url: str) -> str:
    """Trả về domain thuần (vd: example.com) từ URL."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc or parsed.path
        # bỏ www.
        host = re.sub(r"^www\.", "", host)
        return host.strip("/").lower()
    except Exception:
        return ""
