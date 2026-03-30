"""
ai/company_detector.py
──────────────────────────────────────────────────────
LangChain + Groq pipeline to:
  1. Classify if a website belongs to a company (vs blog/personal/gov)
  2. Extract structured company metadata from raw HTML/text

Model: openai/GPT-OSS-20B via Groq API
Docs: https://console.groq.com/docs/models

Usage:
    detector = CompanyDetector()
    result = await detector.detect("example.com", html="<html>...</html>")
    if result.is_company:
        print(result.name, result.industry)
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq

from config.settings import settings

log = logging.getLogger(__name__)


# ── Output schema ──────────────────────────────────────────────────────────────

@dataclass
class CompanyInfo:
    """Structured result from AI company detection."""
    domain: str
    is_company: bool = False
    confidence: float = 0.0        # 0.0 – 1.0

    # Company fields (populated if is_company=True)
    name: Optional[str] = None
    description: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None
    size_estimate: Optional[str] = None  # "1-10", "11-50", "51-200", "201-1000", "1000+"
    founded_year: Optional[int] = None
    employee_count: Optional[int] = None
    website_type: str = "unknown"   # company | ecommerce | blog | personal | gov | other

    # Social / contact hints found on page
    linkedin_url: Optional[str] = None
    twitter_url: Optional[str] = None
    email_hints: list[str] = field(default_factory=list)

    # Meta
    raw_llm_response: Optional[str] = None
    error: Optional[str] = None


# ── Prompts ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a business intelligence analyst.
Given a website domain and a snippet of its HTML/text content, you must:

1. Determine whether this website belongs to a COMPANY (B2B, B2C, startup, enterprise, SMB) or not.
2. If it IS a company website, extract structured information.

IMPORTANT RULES:
- Only mark is_company=true for real business organizations.
- Personal portfolios, blogs, news sites, government sites → is_company=false.
- E-commerce pure storefronts without a clear company brand → website_type=ecommerce, is_company=true if they have a business identity.
- Always respond with valid JSON only, no markdown fences.

Respond with this exact JSON schema:
{
  "is_company": boolean,
  "confidence": float (0.0-1.0),
  "website_type": "company|ecommerce|blog|personal|gov|other",
  "name": string or null,
  "description": string or null (max 200 chars),
  "industry": string or null (e.g. "Software", "Healthcare", "Finance"),
  "country": string or null (ISO 3166-1 alpha-2 e.g. "US", "VN", "GB"),
  "size_estimate": string or null ("1-10"|"11-50"|"51-200"|"201-1000"|"1000+"),
  "founded_year": integer or null,
  "linkedin_url": string or null,
  "twitter_url": string or null,
  "email_hints": list of strings (max 3)
}"""

USER_PROMPT = """Domain: {domain}

Page content (truncated):
{content}

Return JSON only."""


# ── Detector class ─────────────────────────────────────────────────────────────

class CompanyDetector:
    """
    AI-powered company classifier using LangChain + Groq.

    Thread-safe. Can be shared across async tasks.
    """

    def __init__(self):
        self._llm: Optional[ChatGroq] = None
        self._chain = None

    def _get_chain(self):
        """Lazy-init the LangChain chain (avoids import-time API calls)."""
        if self._chain is not None:
            return self._chain

        if not settings.groq_api_key:
            raise RuntimeError(
                "GROQ_API_KEY not set. Add it to .env or environment variables."
            )

        llm = ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            temperature=settings.groq_temperature,
            max_tokens=settings.groq_max_tokens,
        )

        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", USER_PROMPT),
        ])

        # chain: prompt → llm → json parser
        self._chain = prompt | llm | JsonOutputParser()
        return self._chain

    async def detect(
        self,
        domain: str,
        html: str = "",
        title: str = "",
        meta_description: str = "",
    ) -> CompanyInfo:
        """
        Classify a domain. Returns a CompanyInfo dataclass.

        :param domain: bare domain string (e.g. "stripe.com")
        :param html: raw or cleaned HTML of the homepage
        :param title: <title> tag value
        :param meta_description: <meta name=description> value
        """
        # Build a compact content snippet ≤ max chars
        content_parts = []
        if title:
            content_parts.append(f"Title: {title}")
        if meta_description:
            content_parts.append(f"Meta description: {meta_description}")
        if html:
            # Strip tags quickly with regex (good enough for context)
            text = _strip_html_tags(html)
            content_parts.append(text)

        content = "\n".join(content_parts)[: settings.ai_max_html_chars]

        try:
            chain = self._get_chain()
            # Run sync chain in thread pool to not block event loop
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(
                None,
                lambda: chain.invoke({"domain": domain, "content": content}),
            )

            if not isinstance(raw, dict):
                raise ValueError(f"Unexpected LLM output type: {type(raw)}")

            return CompanyInfo(
                domain=domain,
                is_company=bool(raw.get("is_company", False)),
                confidence=float(raw.get("confidence", 0.0)),
                website_type=raw.get("website_type", "unknown"),
                name=raw.get("name"),
                description=raw.get("description"),
                industry=raw.get("industry"),
                country=raw.get("country"),
                size_estimate=raw.get("size_estimate"),
                founded_year=raw.get("founded_year"),
                linkedin_url=raw.get("linkedin_url"),
                twitter_url=raw.get("twitter_url"),
                email_hints=raw.get("email_hints", []),
                raw_llm_response=json.dumps(raw),
            )

        except Exception as exc:
            log.error("AI detection failed for %s: %s", domain, exc)
            return CompanyInfo(domain=domain, error=str(exc))

    async def detect_batch(
        self,
        items: list[dict],  # each: {"domain": str, "html": str, ...}
    ) -> list[CompanyInfo]:
        """
        Detect multiple domains concurrently.

        :param items: list of dicts with keys: domain, html, title, meta_description
        """
        tasks = [
            self.detect(
                domain=item["domain"],
                html=item.get("html", ""),
                title=item.get("title", ""),
                meta_description=item.get("meta_description", ""),
            )
            for item in items
        ]
        return await asyncio.gather(*tasks)


# ── Helpers ────────────────────────────────────────────────────────────────────

import re as _re

_TAG_RE = _re.compile(r"<[^>]+>")
_SPACE_RE = _re.compile(r"\s{2,}")


def _strip_html_tags(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = _TAG_RE.sub(" ", html)
    text = _SPACE_RE.sub(" ", text)
    return text.strip()
