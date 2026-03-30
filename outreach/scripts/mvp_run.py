#!/usr/bin/env python3
"""
scripts/mvp_run.py
──────────────────────────────────────────────────────
MVP quick-start: run the full pipeline in a single process.
No Docker, no Celery — just Python.

Steps:
  1. Collect domains from Google Maps (or use demo domains)
  2. Crawl each with Playwright
  3. AI classify with Groq
  4. Save to SQLite (for MVP)
  5. Print summary

Usage:
    python scripts/mvp_run.py --query "fintech company" --location "Singapore" --limit 20
    python scripts/mvp_run.py --demo         # use built-in demo domains
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mvp")

# Demo domains for testing without API keys
DEMO_DOMAINS = [
    "stripe.com",
    "notion.so",
    "figma.com",
    "linear.app",
    "vercel.com",
    "supabase.com",
    "planetscale.com",
    "retool.com",
    "segment.com",
    "amplitude.com",
]


async def run_mvp(
    query: str,
    location: str,
    limit: int,
    use_demo: bool,
):
    from ai.company_detector import CompanyDetector
    from collectors.google_maps import GoogleMapsCollector
    from crawler.playwright_crawler import PlaywrightCrawler
    from crawler.data_extractor import DataExtractor
    from config.settings import settings

    results = []
    detector = CompanyDetector()
    extractor = DataExtractor()

    # Step 1: Collect domains
    log.info("=== Step 1: Collecting domains ===")
    domains = []

    if use_demo:
        domains = DEMO_DOMAINS[:limit]
        log.info("Using %d demo domains", len(domains))
    else:
        collector = GoogleMapsCollector()
        async for company in collector.collect(query=query, location=location, max_results=limit):
            if company.domain:
                domains.append(company.domain)
                log.info("  + %s (%s)", company.domain, company.name)

    if not domains:
        log.warning("No domains collected. Try --demo flag or check your API key.")
        return

    log.info("Total domains: %d", len(domains))

    # Step 2 + 3: Crawl + AI classify
    log.info("\n=== Step 2-3: Crawl + AI Classify ===")

    async with PlaywrightCrawler(headless=settings.playwright_headless) as crawler:
        for i, domain in enumerate(domains, 1):
            log.info("[%d/%d] Processing: %s", i, len(domains), domain)

            # Crawl
            url = f"https://{domain}"
            page = await crawler.crawl(url, follow_links=["/about"])

            if page.error:
                log.warning("  Crawl failed: %s", page.error)
                continue

            # Heuristic extract
            extracted = extractor.extract(page)

            # AI classify
            if settings.groq_api_key:
                ai_result = await detector.detect(
                    domain,
                    html=page.html,
                    title=page.title,
                    meta_description=page.meta_description,
                )
            else:
                log.warning("  No GROQ_API_KEY — skipping AI classification")
                # Fallback: use heuristics only
                from ai.company_detector import CompanyInfo
                ai_result = CompanyInfo(
                    domain=domain,
                    is_company=extracted.is_likely_company,
                    confidence=len(extracted.signals) / 10.0,
                    name=extracted.company_name_guess,
                    website_type="company" if extracted.is_likely_company else "unknown",
                )

            result = {
                "domain": domain,
                "is_company": ai_result.is_company,
                "confidence": round(ai_result.confidence, 2),
                "name": ai_result.name,
                "industry": ai_result.industry,
                "country": ai_result.country,
                "size": ai_result.size_estimate,
                "emails": page.emails,
                "social_links": page.social_links,
                "heuristic_signals": extracted.signals,
            }
            results.append(result)

            status_icon = "✓" if ai_result.is_company else "✗"
            log.info(
                "  %s %s | confidence=%.2f | name=%s | industry=%s",
                status_icon,
                domain,
                ai_result.confidence,
                ai_result.name or "?",
                ai_result.industry or "?",
            )

    # Step 4: Save results
    log.info("\n=== Step 4: Saving results ===")
    output_path = Path("mvp_results.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    log.info("Results saved to: %s", output_path)

    # Step 5: Summary
    companies = [r for r in results if r["is_company"]]
    log.info("\n=== Summary ===")
    log.info("Total processed:    %d", len(results))
    log.info("Identified company: %d (%.0f%%)", len(companies), 100 * len(companies) / max(len(results), 1))
    log.info("With emails:        %d", sum(1 for r in results if r["emails"]))
    log.info("Results file:       %s", output_path.absolute())


def main():
    parser = argparse.ArgumentParser(description="MVP Company Crawler")
    parser.add_argument("--query", default="software company", help="Search query")
    parser.add_argument("--location", default="", help="Location (city/country)")
    parser.add_argument("--limit", type=int, default=10, help="Max companies to process")
    parser.add_argument("--demo", action="store_true", help="Use built-in demo domains")
    args = parser.parse_args()

    asyncio.run(run_mvp(
        query=args.query,
        location=args.location,
        limit=args.limit,
        use_demo=args.demo,
    ))


if __name__ == "__main__":
    main()
