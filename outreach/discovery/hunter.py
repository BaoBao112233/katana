"""
discovery/hunter.py
Tìm email của công ty theo domain dùng Hunter.io API.
https://hunter.io/api-documentation
"""

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)


class HunterClient:
    """Wrapper đơn giản cho Hunter.io Domain Search API."""

    BASE_URL = "https://api.hunter.io/v2"

    def __init__(self, api_key: str, limit: int = 5,
                 require_verified: bool = False, type_filter: str = ""):
        self.api_key = api_key
        self.limit = limit
        self.require_verified = require_verified
        self.type_filter = type_filter

    def find_emails(self, domain: str, company_name: str = "") -> list[dict]:
        """
        Tìm email theo domain.
        Trả về: [{"email": ..., "first_name": ..., "last_name": ...,
                   "position": ..., "confidence": ...}, ...]
        """
        params: dict = {
            "domain": domain,
            "api_key": self.api_key,
            "limit": self.limit,
        }
        if company_name:
            params["company"] = company_name
        if self.type_filter:
            params["type"] = self.type_filter

        try:
            resp = requests.get(
                f"{self.BASE_URL}/domain-search",
                params=params,
                timeout=15,
            )

            if resp.status_code == 429:
                log.warning("Hunter.io rate-limited. Chờ 30 giây...")
                time.sleep(30)
                return []

            if resp.status_code == 402:
                log.warning("Hunter.io hết quota tháng.")
                return []

            resp.raise_for_status()
            data = resp.json()
            emails_raw = data.get("data", {}).get("emails", [])

            results = []
            for entry in emails_raw:
                if self.require_verified and entry.get("verification", {}).get("status") != "valid":
                    continue
                results.append({
                    "email": entry.get("value", ""),
                    "first_name": entry.get("first_name") or "",
                    "last_name": entry.get("last_name") or "",
                    "position": entry.get("position") or "",
                    "confidence": entry.get("confidence", 0),
                    "source": "hunter",
                })

            log.debug("Hunter: %s → %d emails", domain, len(results))
            return results

        except Exception as e:
            log.error("Hunter.io lỗi cho domain %s: %s", domain, e)
            return []

    def get_account_quota(self) -> Optional[dict]:
        """Kiểm tra quota còn lại của tài khoản."""
        try:
            resp = requests.get(
                f"{self.BASE_URL}/account",
                params={"api_key": self.api_key},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("data", {})
        except Exception as e:
            log.error("Không lấy được quota Hunter.io: %s", e)
            return None
