"""
db/database.py
SQLite — lưu trạng thái công ty, email, lịch sử gửi mail.
Tránh crawl lại / gửi trùng email.
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class Database:
    """Quản lý SQLite database cho hệ thống outreach."""

    SCHEMA = """
    -- Bảng công ty
    CREATE TABLE IF NOT EXISTS companies (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        domain      TEXT    NOT NULL UNIQUE,
        url         TEXT,
        snippet     TEXT,
        source      TEXT,           -- google / manual / csv
        crawled     INTEGER DEFAULT 0,  -- 0=chưa crawl, 1=đã crawl
        created_at  TEXT    NOT NULL
    );

    -- Bảng email tìm được
    CREATE TABLE IF NOT EXISTS emails (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id  INTEGER NOT NULL REFERENCES companies(id),
        email       TEXT    NOT NULL,
        first_name  TEXT,
        last_name   TEXT,
        position    TEXT,
        confidence  INTEGER DEFAULT 0,
        source      TEXT,           -- hunter / crawler / apollo
        UNIQUE(company_id, email)
    );

    -- Lịch sử gửi mail
    CREATE TABLE IF NOT EXISTS send_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        email_id    INTEGER NOT NULL REFERENCES emails(id),
        sent_at     TEXT    NOT NULL,
        status      TEXT    NOT NULL,   -- sent / failed / skipped
        error_msg   TEXT
    );
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #

    def _conn_get(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._conn_get()
        conn.executescript(self.SCHEMA)
        conn.commit()
        log.debug("Database schema sẵn sàng: %s", self.db_path)

    # ------------------------------------------------------------------ #
    # Companies
    # ------------------------------------------------------------------ #

    def upsert_company(self, name: str, domain: str, url: str = "",
                       snippet: str = "", source: str = "") -> int:
        """
        Chèn hoặc cập nhật công ty theo domain.
        Trả về company_id.
        """
        conn = self._conn_get()
        cur = conn.execute(
            """
            INSERT INTO companies (name, domain, url, snippet, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                name    = excluded.name,
                url     = excluded.url,
                snippet = excluded.snippet,
                source  = excluded.source
            RETURNING id
            """,
            (name, domain, url, snippet, source, _now()),
        )
        row = cur.fetchone()
        conn.commit()
        return row[0]

    def get_company_id(self, domain: str) -> Optional[int]:
        cur = self._conn_get().execute(
            "SELECT id FROM companies WHERE domain = ?", (domain,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def mark_crawled(self, company_id: int) -> None:
        conn = self._conn_get()
        conn.execute(
            "UPDATE companies SET crawled = 1 WHERE id = ?", (company_id,)
        )
        conn.commit()

    def has_been_crawled(self, company_id: int) -> bool:
        cur = self._conn_get().execute(
            "SELECT crawled FROM companies WHERE id = ?", (company_id,)
        )
        row = cur.fetchone()
        return bool(row and row[0])

    # ------------------------------------------------------------------ #
    # Emails
    # ------------------------------------------------------------------ #

    def upsert_email(self, company_id: int, email: str,
                     first_name: str = "", last_name: str = "",
                     position: str = "", confidence: int = 0,
                     source: str = "") -> int:
        """Chèn hoặc cập nhật email. Trả về email_id."""
        conn = self._conn_get()
        cur = conn.execute(
            """
            INSERT INTO emails (company_id, email, first_name, last_name,
                                position, confidence, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_id, email) DO UPDATE SET
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                position   = excluded.position,
                confidence = excluded.confidence,
                source     = excluded.source
            RETURNING id
            """,
            (company_id, email.lower(), first_name, last_name,
             position, confidence, source),
        )
        row = cur.fetchone()
        conn.commit()
        return row[0]

    def get_unsent_emails(self) -> list[dict]:
        """
        Trả về danh sách email chưa gửi (chưa có bản ghi sent/failed trong send_log).
        """
        cur = self._conn_get().execute(
            """
            SELECT
                e.id        AS email_id,
                e.email,
                e.first_name,
                e.last_name,
                e.position,
                c.id        AS company_id,
                c.name      AS company_name,
                c.domain,
                c.url       AS company_url
            FROM emails e
            JOIN companies c ON c.id = e.company_id
            WHERE e.id NOT IN (
                SELECT DISTINCT email_id FROM send_log
                WHERE status IN ('sent', 'failed')
            )
            ORDER BY e.confidence DESC, e.id ASC
            """
        )
        return [dict(row) for row in cur.fetchall()]

    def has_email_been_sent(self, email_id: int) -> bool:
        cur = self._conn_get().execute(
            "SELECT 1 FROM send_log WHERE email_id = ? AND status = 'sent'",
            (email_id,),
        )
        return cur.fetchone() is not None

    # ------------------------------------------------------------------ #
    # Send log
    # ------------------------------------------------------------------ #

    def log_send(self, email_id: int, status: str, error_msg: str = "") -> None:
        conn = self._conn_get()
        conn.execute(
            """
            INSERT INTO send_log (email_id, sent_at, status, error_msg)
            VALUES (?, ?, ?, ?)
            """,
            (email_id, _now(), status, error_msg),
        )
        conn.commit()

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #

    def stats(self) -> dict:
        conn = self._conn_get()
        companies   = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        crawled     = conn.execute("SELECT COUNT(*) FROM companies WHERE crawled=1").fetchone()[0]
        emails_total = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        sent        = conn.execute("SELECT COUNT(*) FROM send_log WHERE status='sent'").fetchone()[0]
        failed      = conn.execute("SELECT COUNT(*) FROM send_log WHERE status='failed'").fetchone()[0]
        return {
            "companies": companies,
            "crawled": crawled,
            "emails": emails_total,
            "sent": sent,
            "failed": failed,
        }

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")
