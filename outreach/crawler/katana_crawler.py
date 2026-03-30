"""
crawler/katana_crawler.py
Wrap binary katana để crawl một website và trích xuất email.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

# Suffix / keyword giả (JS lib, template …)
FAKE_EMAIL_PATTERNS = re.compile(
    r"@(example|test|domain|yourdomain|email|schematics|types|"
    r"angular|babel|jest|rollup|webpack|node_modules|localhost)",
    re.IGNORECASE,
)

FAKE_EXTENSION_PATTERNS = re.compile(
    r"\.(png|jpg|gif|css|js|ts|svg|woff|ttf|ico)$",
    re.IGNORECASE,
)


class KatanaCrawler:
    """Chạy katana binary và trả về danh sách email tìm được."""

    def __init__(
        self,
        katana_binary: str = "../katana",
        depth: int = 3,
        concurrency: int = 5,
        rate_limit: int = 20,
        timeout: int = 15,
        headless: bool = False,
    ):
        # Resolve đường dẫn katana tương đối → tuyệt đối
        self.katana_binary = str(
            Path(__file__).parent.parent.parent / katana_binary
            if not os.path.isabs(katana_binary)
            else katana_binary
        )
        self.depth = depth
        self.concurrency = concurrency
        self.rate_limit = rate_limit
        self.timeout = timeout
        self.headless = headless

    def _is_available(self) -> bool:
        return os.path.isfile(self.katana_binary) and os.access(
            self.katana_binary, os.X_OK
        )

    def crawl(self, url: str) -> list[str]:
        """
        Crawl URL bằng katana, trả về danh sách email (đã dedup, đã lọc).
        Nếu katana không khả dụng, trả về [].
        """
        if not self._is_available():
            log.warning(
                "katana binary không tìm thấy tại '%s'. Bỏ qua bước crawl.",
                self.katana_binary,
            )
            return []

        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "crawl.jsonl"
            cmd = self._build_command(url, str(out_file))
            log.debug("Chạy: %s", " ".join(cmd))

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout * 60,  # timeout tổng (phút → giây)
                )
                if proc.returncode not in (0, 1):
                    log.warning("katana exit %d: %s", proc.returncode, proc.stderr[:300])
            except subprocess.TimeoutExpired:
                log.warning("katana timeout khi crawl %s", url)
                return []
            except Exception as e:
                log.error("Lỗi chạy katana: %s", e)
                return []

            return self._parse_emails(out_file)

    # ------------------------------------------------------------------ #

    def _build_command(self, url: str, out_file: str) -> list[str]:
        """Xây dựng lệnh katana."""
        cmd = [
            self.katana_binary,
            "-u", url,
            "-d", str(self.depth),
            "-c", str(self.concurrency),
            "-rl", str(self.rate_limit),
            "-timeout", str(self.timeout),
            "-retry", "1",
            "-jc",          # JavaScript crawl
            "-aff",         # include affiliation links
            "-sf", "email", # extract email field
            "-fs", "rdn",   # scope: root domain name
            "-silent",
            "-j",           # JSON output
            "-o", out_file,
        ]
        if self.headless:
            cmd += ["-headless", "-system-chrome", "-no-sandbox", "-xhr"]
        return cmd

    def _parse_emails(self, jsonl_file: Path) -> list[str]:
        """Trích email từ file JSONL output của katana."""
        if not jsonl_file.exists():
            return []

        raw_emails: set[str] = set()

        # Đọc từng dòng JSON
        with open(jsonl_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Tìm email trực tiếp trong dòng text
                for m in EMAIL_REGEX.finditer(line):
                    raw_emails.add(m.group(0).lower())

                # Parse JSON, tìm trong response body nếu có
                try:
                    obj = json.loads(line)
                    body = obj.get("response", {}).get("body", "")
                    if body:
                        for m in EMAIL_REGEX.finditer(body):
                            raw_emails.add(m.group(0).lower())
                except Exception:
                    pass

        # Lọc email giả
        cleaned = [
            e for e in raw_emails
            if not FAKE_EMAIL_PATTERNS.search(e)
            and not FAKE_EXTENSION_PATTERNS.search(e)
        ]
        return sorted(cleaned)
