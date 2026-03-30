#!/usr/bin/env python3
"""
outreach/main.py
────────────────────────────────────────────────────────────
Hệ thống tự động:
  1. Tìm công ty trên thế giới (Google Custom Search)
  2. Tìm email (Hunter.io + crawl website katana)
  3. Lưu vào SQLite (tránh trùng lặp)
  4. Gửi email outreach đến từng công ty

Cách dùng:
    python main.py                        # chạy toàn bộ pipeline
    python main.py --discover-only        # chỉ tìm công ty + email
    python main.py --send-only            # chỉ gửi email (từ DB)
    python main.py --stats                # xem thống kê
    python main.py --import-csv file.csv  # import danh sách thủ công
────────────────────────────────────────────────────────────
"""

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

import yaml

# ─── thiết lập PYTHONPATH để import package con ──────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

import colorlog  # noqa: E402

from db.database import Database  # noqa: E402
from crawler.katana_crawler import KatanaCrawler  # noqa: E402
from discovery.google_search import GoogleSearchDiscovery  # noqa: E402
from discovery.hunter import HunterClient  # noqa: E402
from email_sender.sender import EmailSender  # noqa: E402


# ─── Logging ─────────────────────────────────────────────────────────────────

def _setup_logging(level_str: str, log_file: str) -> None:
    level = getattr(logging, level_str.upper(), logging.INFO)

    handler = colorlog.StreamHandler()
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
            datefmt="%H:%M:%S",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red,bg_white",
            },
        )
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    )

    logging.basicConfig(level=level, handlers=[handler, file_handler])


log = logging.getLogger("outreach")


# ─── Config loader ───────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── Pipeline steps ──────────────────────────────────────────────────────────

def step_discover(cfg: dict, db: Database) -> int:
    """
    Bước 1: Dùng Google Search để tìm website công ty.
    Trả về số công ty mới thêm vào DB.
    """
    api_cfg = cfg["api"]
    search_cfg = cfg["search"]

    if not api_cfg.get("google_api_key") or api_cfg["google_api_key"].startswith("YOUR_"):
        log.warning("Google API key chưa cấu hình → bỏ qua bước Google Search.")
        return 0

    searcher = GoogleSearchDiscovery(
        api_key=api_cfg["google_api_key"],
        cx=api_cfg["google_cx"],
        country=search_cfg.get("country", ""),
    )

    new_count = 0
    for company in searcher.discover(
        queries=search_cfg["queries"],
        pages_per_query=search_cfg.get("pages_per_query", 3),
    ):
        db.upsert_company(
            name=company["name"],
            domain=company["domain"],
            url=company["url"],
            snippet=company.get("snippet", ""),
            source="google",
        )
        new_count += 1

    log.info("Google Search: tìm thấy %d công ty.", new_count)
    return new_count


def step_find_emails(cfg: dict, db: Database) -> int:
    """
    Bước 2: Với mỗi công ty, tìm email qua Hunter.io và/hoặc katana.
    Trả về tổng số email mới.
    """
    api_cfg = cfg["api"]
    hunter_cfg = cfg.get("hunter", {})
    crawler_cfg = cfg.get("crawler", {})

    use_hunter = (
        api_cfg.get("hunter_api_key")
        and not api_cfg["hunter_api_key"].startswith("YOUR_")
    )
    use_crawler = True  # katana fallback

    hunter: HunterClient | None = None
    if use_hunter:
        hunter = HunterClient(
            api_key=api_cfg["hunter_api_key"],
            limit=hunter_cfg.get("limit", 5),
            require_verified=hunter_cfg.get("require_verified", False),
            type_filter=hunter_cfg.get("type_filter", ""),
        )

    katana = KatanaCrawler(
        katana_binary=crawler_cfg.get("katana_binary", "../katana"),
        depth=crawler_cfg.get("depth", 3),
        concurrency=crawler_cfg.get("concurrency", 5),
        rate_limit=crawler_cfg.get("rate_limit", 20),
        timeout=crawler_cfg.get("timeout", 15),
        headless=crawler_cfg.get("headless", False),
    )

    conn = db._conn_get()
    companies = conn.execute(
        "SELECT id, name, domain, url FROM companies WHERE crawled = 0"
    ).fetchall()

    if not companies:
        log.info("Không có công ty mới cần tìm email.")
        return 0

    log.info("Tìm email cho %d công ty...", len(companies))
    total_emails = 0

    for row in companies:
        cid = row["id"]
        cname = row["name"]
        domain = row["domain"]
        url = row["url"] or f"https://{domain}"

        log.info("── %s (%s)", cname, domain)

        # Hunter.io
        if hunter:
            emails = hunter.find_emails(domain, company_name=cname)
            for e in emails:
                db.upsert_email(
                    company_id=cid,
                    email=e["email"],
                    first_name=e.get("first_name", ""),
                    last_name=e.get("last_name", ""),
                    position=e.get("position", ""),
                    confidence=e.get("confidence", 0),
                    source="hunter",
                )
                total_emails += 1

        # Katana crawler
        if use_crawler:
            emails_raw = katana.crawl(url)
            for email_addr in emails_raw:
                db.upsert_email(
                    company_id=cid,
                    email=email_addr,
                    source="crawler",
                )
                total_emails += 1

        db.mark_crawled(cid)

    log.info("Tìm được tổng cộng %d email mới.", total_emails)
    return total_emails


def step_send_emails(cfg: dict, db: Database) -> int:
    """
    Bước 3: Gửi email đến danh sách chưa gửi trong DB.
    Trả về số email gửi thành công.
    """
    smtp_cfg = cfg["smtp"]
    tpl_cfg = cfg["email_template"]

    # Kiểm tra cấu hình SMTP
    if smtp_cfg["password"].startswith("YOUR_"):
        log.error("SMTP password chưa cấu hình trong config.yaml. Bỏ qua bước gửi email.")
        return 0

    sender = EmailSender(
        host=smtp_cfg["host"],
        port=smtp_cfg["port"],
        sender_email=smtp_cfg["sender_email"],
        sender_name=smtp_cfg.get("sender_name", ""),
        password=smtp_cfg["password"],
        use_tls=smtp_cfg.get("use_tls", True),
        delay_between_emails=smtp_cfg.get("delay_between_emails", 5),
        daily_limit=smtp_cfg.get("daily_limit", 100),
    )

    unsent = db.get_unsent_emails()
    if not unsent:
        log.info("Không có email nào cần gửi.")
        return 0

    log.info("Chuẩn bị gửi %d email...", len(unsent))
    sent_count = 0

    for record in unsent:
        # Biến template
        recipient_name = " ".join(
            filter(None, [record.get("first_name", ""), record.get("last_name", "")])
        ).strip() or "Team"

        variables = {
            "company_name": record["company_name"],
            "company_url": record["company_url"] or f"https://{record['domain']}",
            "recipient_email": record["email"],
            "recipient_name": recipient_name,
            "sender_name": tpl_cfg.get("your_name", smtp_cfg["sender_name"]),
            "sender_email": smtp_cfg["sender_email"],
            "your_company": tpl_cfg.get("your_company", ""),
            "your_title": tpl_cfg.get("your_title", ""),
            "your_website": tpl_cfg.get("your_website", ""),
        }

        success = sender.send(
            to_email=record["email"],
            subject=tpl_cfg.get("subject", "Partnership Opportunity with {company_name}"),
            template_file=tpl_cfg.get("template_file", "default.html"),
            template_vars=variables,
        )

        status = "sent" if success else "failed"
        db.log_send(email_id=record["email_id"], status=status)

        if success:
            sent_count += 1
        else:
            # Nếu lỗi xác thực SMTP → dừng hẳn
            break

    log.info("Gửi xong: %d/%d email thành công.", sent_count, len(unsent))
    return sent_count


def step_import_csv(csv_path: str, db: Database) -> int:
    """
    Import danh sách công ty từ CSV.
    Cột bắt buộc: domain
    Cột tuỳ chọn: name, url, email, first_name, last_name, position
    """
    if not os.path.isfile(csv_path):
        log.error("File CSV không tồn tại: %s", csv_path)
        return 0

    count = 0
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            domain = row.get("domain", "").strip().lower()
            if not domain:
                continue

            cid = db.upsert_company(
                name=row.get("name", domain),
                domain=domain,
                url=row.get("url", f"https://{domain}"),
                source="csv",
            )

            email = row.get("email", "").strip().lower()
            if email and "@" in email:
                db.upsert_email(
                    company_id=cid,
                    email=email,
                    first_name=row.get("first_name", ""),
                    last_name=row.get("last_name", ""),
                    position=row.get("position", ""),
                    source="csv",
                )
            count += 1

    log.info("Imported %d dòng từ %s", count, csv_path)
    return count


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Katana Outreach — tự động tìm công ty & gửi email"
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Đường dẫn file config.yaml (mặc định: config.yaml)"
    )
    parser.add_argument(
        "--discover-only", action="store_true",
        help="Chỉ chạy bước tìm công ty + email, không gửi mail"
    )
    parser.add_argument(
        "--send-only", action="store_true",
        help="Chỉ gửi email từ DB (không tìm thêm)"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Hiển thị thống kê từ DB"
    )
    parser.add_argument(
        "--import-csv", metavar="FILE",
        help="Import danh sách công ty/email từ CSV"
    )
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[ERROR] Không tìm thấy file config: {config_path}")
        print("        Hãy copy và chỉnh sửa outreach/config.yaml trước.")
        sys.exit(1)

    cfg = load_config(str(config_path))

    # Logging
    log_cfg = cfg.get("logging", {})
    log_file = log_cfg.get("file", "data/outreach.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    _setup_logging(log_cfg.get("level", "INFO"), log_file)

    # Database
    db = Database(cfg["database"]["path"])

    try:
        # ── Stats ──
        if args.stats:
            s = db.stats()
            print("\n══════════════════════════════════")
            print("  THỐNG KÊ OUTREACH DATABASE")
            print("══════════════════════════════════")
            print(f"  Công ty đã tìm : {s['companies']}")
            print(f"  Đã crawl       : {s['crawled']}")
            print(f"  Email tìm được : {s['emails']}")
            print(f"  Email đã gửi   : {s['sent']}")
            print(f"  Email thất bại : {s['failed']}")
            print("══════════════════════════════════\n")
            return

        # ── Import CSV ──
        if args.import_csv:
            step_import_csv(args.import_csv, db)
            return

        # ── Full pipeline hoặc từng phần ──
        if not args.send_only:
            log.info("═══ BƯỚC 1: Tìm kiếm công ty (Google Search) ═══")
            step_discover(cfg, db)

            log.info("═══ BƯỚC 2: Tìm email (Hunter.io + Katana) ═══")
            step_find_emails(cfg, db)

        if not args.discover_only:
            log.info("═══ BƯỚC 3: Gửi email ═══")
            step_send_emails(cfg, db)

        # Tóm tắt cuối
        s = db.stats()
        log.info(
            "Hoàn tất. Công ty: %d | Email: %d | Đã gửi: %d",
            s["companies"], s["emails"], s["sent"],
        )

    finally:
        db.close()


if __name__ == "__main__":
    main()
