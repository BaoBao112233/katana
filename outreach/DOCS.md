# Company Database Crawler — Architecture & Design Doc

> Mục tiêu: Crawl 10M+ website, AI auto-detect company site, build SaaS kiểu Crunchbase.

---

## 1. Nguyên tắc cốt lõi: Đừng crawl web thô → Crawl "nguồn danh sách"

Thay vì duyệt toàn bộ internet, hãy **bắt đầu từ nguồn có cấu trúc** rồi map sang website.

### 1.1 Các nguồn dữ liệu ưu tiên

| Nguồn | Dữ liệu có | Phương pháp |
|---|---|---|
| Google Maps | Tên, địa chỉ, website, phone | API / scraping |
| LinkedIn Company | Tên, ngành, size, website | API / scraping |
| Common Crawl | Domain + HTML sẵn có | S3 download |
| OpenCorporates | Tên đăng ký, quốc gia | API công khai |
| Whois Database | Domain + registrant info | Bulk download |
| Yellow Pages / VN: dangkykinhdoanh | Tên, ngành, địa chỉ | Scraping |

**Lý do:** Dataset có cấu trúc đã có `name + website` → bỏ qua bước tìm domain.
Common Crawl miễn phí ~80B trang, download S3 → xử lý offline.

---

## 2. Pipeline tổng thể (Data Engineering Standard)

```
[Bước 1] Thu thập domain/công ty          <- collectors/
       ↓
[Bước 2] Chuẩn hóa + dedup tên/domain    <- utils/domain_utils.py
       ↓
[Bước 3] AI phân loại: có phải công ty?  <- ai/company_detector.py
       ↓
[Bước 4] Crawl website → extract info    <- crawler/playwright_crawler.py
       ↓
[Bước 5] Enrich: email, social, contacts <- workers/enrichment_worker.py
       ↓
[Bước 6] Lưu trữ + index search          <- db/ + Elasticsearch
       ↓
[Bước 7] Expose qua SaaS API             <- api/ (FastAPI)
```

### 2.1 Chi tiết từng bước

**Bước 1 — Thu thập**
- Google Maps scraping theo category + quốc gia
- Common Crawl domain list (miễn phí, bulk)
- Domain inference: `companyname.com`, `company-name.com` → check DNS

**Bước 2 — Chuẩn hóa**
- Normalize: lowercase, remove suffix (LLC, Inc, Co.,...)
- Fuzzy matching: Levenshtein distance, sentence-embedding
- Domain normalization: strip `www.`, subdomains

**Bước 3 — AI Detection (LangChain + Groq)**
- LLM call với HTML/metadata của trang
- Phân loại: `company | personal | blog | ecommerce | other`
- Extract: tên, mô tả, ngành, country, size estimate

**Bước 4 — Crawl**
- Playwright cho JS-heavy site
- Katana binary cho tốc độ cao
- Chỉ crawl: homepage + `/about` + `/contact`

**Bước 5 — Enrich**
- Hunter.io / Apollo: tìm email người liên hệ
- Social links: LinkedIn, Twitter, Facebook
- Tech stack detection: Wappalyzer

**Bước 6 — Storage**
- PostgreSQL: metadata công ty
- Elasticsearch: full-text search
- S3/MinIO: lưu raw HTML

**Bước 7 — SaaS API**
- FastAPI: REST API
- Endpoint: search, filter, export
- Auth: API key per user

---

## 3. Kiến trúc hệ thống

### 3.1 MVP (dưới 1M sites, 1 server)

```
[Collectors]  →  [Redis Queue]  →  [Celery Workers]
                                        ↓
                               [AI Detector (Groq)]
                                        ↓
                               [Playwright Crawler]
                                        ↓
                               [PostgreSQL + Elasticsearch]
                                        ↓
                               [FastAPI SaaS]
```

**Stack:** Python · FastAPI · Celery · Redis · PostgreSQL · Playwright · LangChain · Groq

### 3.2 Production Scale (10M+ sites)

```
[Multi-source Collectors]
         ↓
[Kafka Topics: raw_domains / to_crawl / crawled]
         ↓
[Worker Cluster (Kubernetes)]
  ├── AI Classifier Pods  (LangChain + Groq GPT-OSS-20B)
  ├── Crawler Pods        (Playwright + Proxy Rotation)
  └── Enrichment Pods     (Hunter, Apollo, Clearbit)
         ↓
[Storage Layer]
  ├── PostgreSQL (metadata)
  ├── Elasticsearch (search)
  └── S3 / MinIO (raw HTML)
         ↓
[FastAPI SaaS + CDN]
```

**Additions vs MVP:** Kafka · Kubernetes · Proxy rotation · Browser fingerprinting · Horizontal scaling

---

## 4. Scaling Checklist

| Vấn đề | Giải pháp |
|---|---|
| Rate limit / block | Proxy rotation (residential) + random delay |
| JS-heavy sites | Playwright headless + random fingerprint |
| CAPTCHA | 2Captcha API / skip + retry |
| Trùng lặp | Content hash + domain normalization |
| Throughput | Celery + Kafka + horizontal scaling |
| Anti-bot | Randomize UA, accept-language, viewport |

---

## 5. Giới hạn pháp lý & ethical

| Hạn chế | Xử lý |
|---|---|
| `robots.txt` | Parse và tuân thủ ở production |
| GDPR (EU data) | Chỉ lưu public business data, không PII |
| ToS LinkedIn/Google | Dùng API chính thức khi có thể |
| Rate limit | Exponential backoff + jitter |

**Rule:** Chỉ crawl public business information, không crawl dữ liệu cá nhân.

---

## 6. Nguồn dữ liệu miễn phí (Pro tip)

| Nguồn | Dữ liệu | Link |
|---|---|---|
| Common Crawl | 80B pages, monthly dump | commoncrawl.org |
| Majestic Million | Top 1M domains | majestic.com |
| Tranco | Domain rank list | tranco-list.eu |
| OpenCorporates | 200M+ companies | opencorporates.com/api |
| Wikidata | Company knowledge graph | wikidata.org |

---

## 7. Source Code Structure

```
outreach/
├── config/
│   └── settings.py          # Pydantic settings, env vars
├── ai/
│   └── company_detector.py  # LangChain + Groq (GPT-OSS-20B)
├── collectors/
│   ├── google_maps.py       # Google Maps business scraper
│   ├── common_crawl.py      # Common Crawl domain reader
│   └── domain_generator.py  # Domain inference + DNS check
├── crawler/
│   ├── playwright_crawler.py # JS-capable crawler
│   └── data_extractor.py    # Extract company info from HTML
├── workers/
│   ├── celery_app.py        # Celery + Redis config
│   └── tasks.py             # Distributed tasks
├── db/
│   └── models.py            # SQLAlchemy models (PostgreSQL)
├── api/
│   ├── main.py              # FastAPI SaaS app
│   └── routers/
│       ├── companies.py     # CRUD endpoints
│       └── search.py        # Search + filter
├── utils/
│   ├── domain_utils.py      # Domain normalize, DNS check
│   └── proxy_rotator.py     # Proxy pool management
├── docker-compose.yml       # Full stack: PG + Redis + ES + App
└── .env.example             # All env vars
```

---

## 8. Roadmap MVP → Production

### Phase 1 — MVP (1–2 tuần)
- [ ] Google Maps collector → 100K companies
- [ ] AI detector phân loại basic
- [ ] Playwright crawl homepage + about
- [ ] FastAPI với 3 endpoint: list / search / detail

### Phase 2 — Scale (1 tháng)
- [ ] Common Crawl integration → 10M domains
- [ ] Celery distributed workers
- [ ] Proxy rotation
- [ ] Elasticsearch search

### Phase 3 — SaaS (2–3 tháng)
- [ ] User auth + API key management
- [ ] Export CSV/JSON
- [ ] Webhook notifications
- [ ] Dashboard frontend (Next.js)
- [ ] Billing (Stripe)
