"""
utils/domain_utils.py
──────────────────────────────────────────────────────
Domain normalization, validation, DNS checking, and deduplication utilities.
Used across collectors, crawler, and workers.
"""

import asyncio
import hashlib
import logging
import re
import socket
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Suffixes to strip when normalizing company names
COMPANY_SUFFIXES = re.compile(
    r"\b(llc|ltd|inc|corp|co|gmbh|bv|sa|srl|s\.a\.|pvt|"
    r"joint stock|jsc|pty|ag|nv|plc|aps|oy|ab|as|sas|"
    r"công ty|cty|tnhh|cp|hh)\b[\.,]?",
    re.IGNORECASE,
)


def normalize_domain(url_or_domain: str) -> Optional[str]:
    """
    Normalize a URL or bare domain to a clean lowercase domain string.

    Examples:
        "https://www.Example.COM/page" -> "example.com"
        "WWW.Company.io"              -> "company.io"
        "not-a-domain"                -> None
    """
    s = url_or_domain.strip()
    if not s:
        return None

    # Add scheme if missing so urlparse works
    if not s.startswith(("http://", "https://")):
        s = "http://" + s

    try:
        parsed = urlparse(s)
        domain = parsed.netloc or parsed.path
        domain = domain.lower().strip()
        # Strip www.
        if domain.startswith("www."):
            domain = domain[4:]
        # Strip port
        domain = domain.split(":")[0]
        # Basic validation: must have a dot and no spaces
        if "." not in domain or " " in domain:
            return None
        return domain
    except Exception:
        return None


def normalize_company_name(name: str) -> str:
    """
    Lowercase + remove legal suffixes for fuzzy matching.

    "Apple Inc." -> "apple"
    "Công ty TNHH ABC" -> "công ty abc"
    """
    name = name.strip()
    name = COMPANY_SUFFIXES.sub("", name)
    # Remove punctuation except hyphens inside words
    name = re.sub(r"[^\w\s\-]", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name).strip().lower()
    return name


def domain_to_url(domain: str) -> str:
    """Return https://domain if domain has no scheme."""
    if not domain.startswith(("http://", "https://")):
        return f"https://{domain}"
    return domain


def content_hash(text: str) -> str:
    """MD5 hash of text content for dedup."""
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()


def generate_domain_candidates(company_name: str) -> list[str]:
    """
    Generate likely domain candidates given a raw company name.

    "My Company Ltd" -> ["mycompany.com", "my-company.com", "mycompany.io", ...]
    """
    base = normalize_company_name(company_name)
    # Remove spaces → slug
    slug_no_space = re.sub(r"\s+", "", base)
    slug_hyphen = re.sub(r"\s+", "-", base)

    tlds = ["com", "io", "co", "net", "org"]
    candidates = []
    for tld in tlds:
        candidates.append(f"{slug_no_space}.{tld}")
        if slug_hyphen != slug_no_space:
            candidates.append(f"{slug_hyphen}.{tld}")
    return candidates


async def check_domain_dns(domain: str) -> bool:
    """
    Async DNS lookup. Returns True if domain resolves.
    Uses a thread-pool to avoid blocking the event loop.
    """
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, socket.getaddrinfo, domain, None)
        return True
    except socket.gaierror:
        return False


async def find_live_domain(company_name: str) -> Optional[str]:
    """
    Generate domain candidates for a company and return the first that resolves DNS.
    Returns None if no candidate resolves.
    """
    candidates = generate_domain_candidates(company_name)
    tasks = {domain: check_domain_dns(domain) for domain in candidates}

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for domain, result in zip(tasks.keys(), results):
        if result is True:
            log.debug("DNS resolved: %s", domain)
            return domain
    return None


def deduplicate_domains(domains: list[str]) -> list[str]:
    """Return unique normalized domains, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for d in domains:
        norm = normalize_domain(d)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def extract_domain_from_email(email: str) -> Optional[str]:
    """'alice@example.com' -> 'example.com'"""
    parts = email.strip().split("@")
    if len(parts) == 2:
        return normalize_domain(parts[1])
    return None
