"""
crawler/playwright_crawler.py
──────────────────────────────────────────────────────
Async Playwright-based crawler for JS-heavy websites.
Extracts title, meta description, visible text, and social links.

Features:
  - Random user-agent & viewport per request (anti-bot)
  - Configurable proxy support
  - Timeout + retry with exponential backoff
  - Only loads HTML (blocks images, fonts, media → faster)

Usage:
    async with PlaywrightCrawler() as crawler:
        page_data = await crawler.crawl("https://stripe.com")
        print(page_data.title, page_data.meta_description)
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config.settings import settings
from utils.domain_utils import normalize_domain

log = logging.getLogger(__name__)


# ── Result types ───────────────────────────────────────────────────────────────

@dataclass
class PageData:
    """Crawled page data."""
    url: str
    domain: str
    status_code: int = 0
    title: str = ""
    meta_description: str = ""
    text_content: str = ""           # visible text, stripped
    html: str = ""                   # raw HTML (first 50KB)
    emails: list[str] = field(default_factory=list)
    phone_numbers: list[str] = field(default_factory=list)
    social_links: dict[str, str] = field(default_factory=dict)   # {platform: url}
    internal_links: list[str] = field(default_factory=list)
    error: Optional[str] = None
    crawl_time_ms: int = 0


# ── Random browser fingerprints ────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 800},
    {"width": 1366, "height": 768},
]

SOCIAL_DOMAINS = {
    "linkedin.com": "linkedin",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "github.com": "github",
    "youtube.com": "youtube",
}

import re as _re
EMAIL_RE = _re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = _re.compile(r"[\+\(]?[1-9][0-9 \-\(\)]{8,}[0-9]")
FAKE_EMAIL_RE = _re.compile(
    r"@(example|test|domain|yourdomain|sentry|localhost|angular|babel)",
    _re.IGNORECASE,
)


# ── Crawler ────────────────────────────────────────────────────────────────────

class PlaywrightCrawler:
    """
    Async context manager for Playwright-based crawling.

    async with PlaywrightCrawler() as crawler:
        data = await crawler.crawl("https://example.com")
    """

    def __init__(
        self,
        headless: bool = True,
        proxy_url: Optional[str] = None,
        timeout_ms: int = 30_000,
        max_retries: int = 2,
    ):
        self.headless = headless
        self.proxy_url = proxy_url
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries
        self._playwright = None
        self._browser: Optional[Browser] = None

    async def __aenter__(self) -> "PlaywrightCrawler":
        self._playwright = await async_playwright().start()
        launch_opts = {"headless": self.headless}
        if self.proxy_url:
            launch_opts["proxy"] = {"server": self.proxy_url}
        self._browser = await self._playwright.chromium.launch(**launch_opts)
        return self

    async def __aexit__(self, *_):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def crawl(
        self,
        url: str,
        follow_links: Optional[list[str]] = None,
    ) -> PageData:
        """
        Crawl a URL and return structured PageData.

        :param url: target URL (with scheme)
        :param follow_links: additional paths to also crawl on the same domain
                             e.g. ["/about", "/contact"]
        """
        import time
        start = time.monotonic()
        domain = normalize_domain(url) or url

        for attempt in range(self.max_retries + 1):
            try:
                context = await self._new_context()
                try:
                    data = await self._crawl_page(context, url, domain)
                    data.crawl_time_ms = int((time.monotonic() - start) * 1000)

                    # Optionally crawl sub-pages (about, contact)
                    if follow_links and not data.error:
                        await self._enrich_with_subpages(
                            context, url, domain, follow_links, data
                        )
                    return data
                finally:
                    await context.close()

            except Exception as e:
                log.warning("Crawl attempt %d failed for %s: %s", attempt + 1, url, e)
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** attempt)  # exp backoff

        return PageData(url=url, domain=domain, error="Max retries exceeded")

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _new_context(self) -> BrowserContext:
        return await self._browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport=random.choice(VIEWPORTS),
            locale="en-US",
            timezone_id="America/New_York",
            java_script_enabled=True,
        )

    async def _crawl_page(
        self, context: BrowserContext, url: str, domain: str
    ) -> PageData:
        page: Page = await context.new_page()

        # Block heavy resources → speed
        await page.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("image", "font", "media", "stylesheet")
                else route.continue_()
            ),
        )

        try:
            resp = await page.goto(
                url,
                timeout=self.timeout_ms,
                wait_until="domcontentloaded",
            )
            status = resp.status if resp else 0
        except Exception as e:
            await page.close()
            return PageData(url=url, domain=domain, error=str(e))

        # Wait a bit for dynamic content
        await asyncio.sleep(random.uniform(0.5, 1.5))

        html = await page.content()
        title = await page.title()
        meta_desc = await page.evaluate(
            "() => document.querySelector('meta[name=\"description\"]')?.content || ''"
        )
        text = await page.evaluate("() => document.body?.innerText || ''")
        links = await page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href).slice(0, 200)"
        )

        await page.close()

        emails = _extract_emails(text + html)
        phones = _extract_phones(text)
        socials = _extract_socials(links)
        internal = _filter_internal_links(links, domain)

        return PageData(
            url=url,
            domain=domain,
            status_code=status,
            title=title or "",
            meta_description=meta_desc or "",
            text_content=text[:5000],
            html=html[:50_000],
            emails=emails,
            phone_numbers=phones,
            social_links=socials,
            internal_links=internal[:50],
        )

    async def _enrich_with_subpages(
        self,
        context: BrowserContext,
        base_url: str,
        domain: str,
        paths: list[str],
        data: PageData,
    ) -> None:
        """Crawl sub-pages and merge extracted data into main PageData."""
        parsed = urlparse(base_url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        for path in paths:
            sub_url = urljoin(base, path)
            try:
                sub = await self._crawl_page(context, sub_url, domain)
                if not sub.error:
                    data.emails = list(set(data.emails + sub.emails))
                    data.phone_numbers = list(set(data.phone_numbers + sub.phone_numbers))
                    data.social_links.update(sub.social_links)
            except Exception as e:
                log.debug("Sub-page %s failed: %s", sub_url, e)


# ── Extraction helpers ─────────────────────────────────────────────────────────

def _extract_emails(text: str) -> list[str]:
    emails = EMAIL_RE.findall(text)
    return list({
        e.lower() for e in emails
        if not FAKE_EMAIL_RE.search(e)
    })[:10]


def _extract_phones(text: str) -> list[str]:
    return list(set(PHONE_RE.findall(text)))[:5]


def _extract_socials(links: list[str]) -> dict[str, str]:
    socials: dict[str, str] = {}
    for link in links:
        for domain_key, platform in SOCIAL_DOMAINS.items():
            if domain_key in link and platform not in socials:
                socials[platform] = link
    return socials


def _filter_internal_links(links: list[str], domain: str) -> list[str]:
    result = []
    for link in links:
        d = normalize_domain(link)
        if d == domain:
            result.append(link)
    return result
