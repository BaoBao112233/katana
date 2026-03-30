"""
utils/proxy_rotator.py
──────────────────────────────────────────────────────
Thread-safe proxy pool with round-robin + failure tracking.
Supports HTTP proxies from a file or a static list.

Usage:
    rotator = ProxyRotator.from_file("proxies.txt")
    proxy_url = rotator.next()          # "http://user:pass@1.2.3.4:8080"

    async with aiohttp.ClientSession() as s:
        async with s.get(url, proxy=proxy_url) as resp:
            ...

    rotator.mark_failed(proxy_url)      # auto-remove after threshold
"""

import logging
import random
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class ProxyRotator:
    """Round-robin proxy pool with auto-eviction on failures."""

    def __init__(
        self,
        proxies: list[str],
        max_failures: int = 3,
        shuffle: bool = True,
    ):
        self._all = list(proxies)
        self._active: list[str] = list(proxies)
        self._failures: dict[str, int] = defaultdict(int)
        self._max_failures = max_failures
        self._lock = threading.Lock()
        self._idx = 0

        if shuffle:
            random.shuffle(self._active)

        log.info("ProxyRotator initialized with %d proxies", len(self._active))

    # ── Factory methods ────────────────────────────────────────────────────────

    @classmethod
    def from_file(cls, path: str, **kwargs) -> "ProxyRotator":
        """
        Load proxies from a newline-separated text file.
        Format per line: http://user:pass@host:port  OR  host:port
        """
        p = Path(path)
        if not p.exists():
            log.warning("Proxy file not found: %s — using no proxy", path)
            return cls([], **kwargs)

        proxies = []
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if not line.startswith("http"):
                    line = f"http://{line}"
                proxies.append(line)

        log.info("Loaded %d proxies from %s", len(proxies), path)
        return cls(proxies, **kwargs)

    @classmethod
    def from_list(cls, proxies: list[str], **kwargs) -> "ProxyRotator":
        return cls(proxies, **kwargs)

    @classmethod
    def disabled(cls) -> "ProxyRotator":
        """Return an empty rotator (no-op, always returns None)."""
        return cls([])

    # ── Core methods ───────────────────────────────────────────────────────────

    def next(self) -> Optional[str]:
        """Return the next proxy URL, or None if pool is empty."""
        with self._lock:
            if not self._active:
                return None
            proxy = self._active[self._idx % len(self._active)]
            self._idx += 1
            return proxy

    def random(self) -> Optional[str]:
        """Return a random proxy URL from the active pool."""
        with self._lock:
            if not self._active:
                return None
            return random.choice(self._active)

    def mark_failed(self, proxy: str) -> None:
        """Increment failure count; evict proxy if threshold reached."""
        with self._lock:
            self._failures[proxy] += 1
            if self._failures[proxy] >= self._max_failures:
                if proxy in self._active:
                    self._active.remove(proxy)
                    log.warning(
                        "Evicted proxy %s after %d failures. Pool size: %d",
                        proxy, self._failures[proxy], len(self._active),
                    )

    def mark_success(self, proxy: str) -> None:
        """Reset failure count on success."""
        with self._lock:
            self._failures[proxy] = 0

    def size(self) -> int:
        with self._lock:
            return len(self._active)

    def is_empty(self) -> bool:
        return self.size() == 0

    def reload(self, proxies: list[str]) -> None:
        """Hot-reload proxy list without losing failure state."""
        with self._lock:
            self._active = [p for p in proxies if self._failures.get(p, 0) < self._max_failures]
            self._idx = 0
            log.info("Proxy pool reloaded: %d active", len(self._active))
