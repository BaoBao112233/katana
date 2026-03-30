"""
crawler/data_extractor.py
──────────────────────────────────────────────────────
Extract structured company information from crawled PageData.
Works alongside the AI detector: this does regex/heuristic extraction,
AI confirms and fills gaps.

Usage:
    extractor = DataExtractor()
    info = extractor.extract(page_data)
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from crawler.playwright_crawler import PageData

log = logging.getLogger(__name__)

# ── Heuristic patterns ─────────────────────────────────────────────────────────

FOUNDED_RE = re.compile(
    r"(?:founded|established|since|est\.?)\s*[in]?\s*(\d{4})",
    re.IGNORECASE,
)

EMPLOYEE_RE = re.compile(
    r"(\d[\d,]+)\+?\s*(?:employees|team members|people|professionals|staff)",
    re.IGNORECASE,
)

# Match things like "© 2024 CompanyName" or "CompanyName Inc. All rights reserved"
COPYRIGHT_RE = re.compile(
    r"©\s*\d{4}\s+([A-Z][A-Za-z0-9 &,\.\-]{2,60}?)(?:\s*\.|All rights|,|\|)",
    re.IGNORECASE,
)

TECH_KEYWORDS = {
    "SaaS", "Cloud", "AI", "Machine Learning", "API", "Platform",
    "Software", "Fintech", "EdTech", "HealthTech", "E-commerce",
    "CRM", "ERP", "Cybersecurity", "DevOps", "Analytics",
}

SIZE_PATTERNS = [
    (re.compile(r"\b1\s*[-–]\s*10\b|\bunder 10\b", re.I), "1-10"),
    (re.compile(r"\b11\s*[-–]\s*50\b|\b(small team|startup)\b", re.I), "11-50"),
    (re.compile(r"\b51\s*[-–]\s*200\b|\bmid.?size\b", re.I), "51-200"),
    (re.compile(r"\b201\s*[-–]\s*1000\b|\bmid.?market\b", re.I), "201-1000"),
    (re.compile(r"\b1[,\s]?000\+?\b|\benterprise\b|\blarge company\b", re.I), "1000+"),
]


@dataclass
class ExtractedCompanyInfo:
    """Heuristic-extracted fields from page content."""
    company_name_guess: Optional[str] = None
    founded_year: Optional[int] = None
    employee_count_text: Optional[str] = None
    size_estimate: Optional[str] = None
    tech_keywords_found: list[str] = field(default_factory=list)
    has_contact_form: bool = False
    has_pricing_page: bool = False
    is_likely_company: bool = False
    signals: list[str] = field(default_factory=list)   # debug: why is_likely_company


class DataExtractor:
    """
    Rule-based extractor for company signals in page data.
    Complements the LLM-based AI detector.
    """

    def extract(self, page: PageData) -> ExtractedCompanyInfo:
        text = (page.text_content + " " + page.meta_description + " " + page.title).lower()
        full_text = page.text_content + " " + page.html

        info = ExtractedCompanyInfo()

        info.founded_year = self._extract_founded(full_text)
        info.employee_count_text, info.size_estimate = self._extract_size(full_text)
        info.tech_keywords_found = self._find_tech_keywords(full_text)
        info.company_name_guess = self._guess_company_name(page)
        info.has_contact_form = self._has_contact_form(page.html)
        info.has_pricing_page = any("/pricing" in l or "/plans" in l for l in page.internal_links)

        # Heuristic: is this a company site?
        signals = []
        if page.emails:
            signals.append("has_email")
        if info.has_contact_form:
            signals.append("has_contact_form")
        if info.founded_year:
            signals.append("has_founded_year")
        if info.size_estimate:
            signals.append("has_size")
        if len(info.tech_keywords_found) >= 2:
            signals.append("tech_keywords")
        if info.has_pricing_page:
            signals.append("has_pricing")
        if page.social_links.get("linkedin"):
            signals.append("has_linkedin")
        if "about" in text[:200] or "our mission" in text or "our team" in text:
            signals.append("about_language")

        info.signals = signals
        info.is_likely_company = len(signals) >= 2

        return info

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _extract_founded(self, text: str) -> Optional[int]:
        m = FOUNDED_RE.search(text)
        if m:
            year = int(m.group(1))
            if 1800 < year <= 2030:
                return year
        return None

    def _extract_size(self, text: str) -> tuple[Optional[str], Optional[str]]:
        m = EMPLOYEE_RE.search(text)
        raw = m.group(0) if m else None

        for pattern, label in SIZE_PATTERNS:
            if pattern.search(text):
                return raw, label

        if m:
            # Try to infer from count
            digits = re.sub(r"[^\d]", "", m.group(1))
            if digits:
                n = int(digits)
                if n <= 10:
                    return raw, "1-10"
                elif n <= 50:
                    return raw, "11-50"
                elif n <= 200:
                    return raw, "51-200"
                elif n <= 1000:
                    return raw, "201-1000"
                else:
                    return raw, "1000+"

        return None, None

    def _find_tech_keywords(self, text: str) -> list[str]:
        found = []
        text_lower = text.lower()
        for kw in TECH_KEYWORDS:
            if kw.lower() in text_lower:
                found.append(kw)
        return found[:10]

    def _guess_company_name(self, page: PageData) -> Optional[str]:
        # Try copyright notice
        m = COPYRIGHT_RE.search(page.html)
        if m:
            return m.group(1).strip()
        # Fallback to title
        if page.title:
            # Remove " - Company Name" suffix patterns
            name = re.split(r"[\|—\-]", page.title)[0].strip()
            if name:
                return name
        return None

    def _has_contact_form(self, html: str) -> bool:
        return bool(re.search(
            r'<form[^>]*>.*?(contact|message|inquiry|enquiry).*?</form>',
            html,
            re.IGNORECASE | re.DOTALL,
        ))
