# Company Database — SaaS Crawler

> Hệ thống crawler 10M+ website · AI auto-detect company site · SaaS API kiểu Crunchbase

## Kiến trúc

```
outreach/
├── main.py                    ← Điểm vào chính
├── config.yaml                ← Cấu hình (API keys, SMTP, template...)
├── requirements.txt
├── data/                      ← SQLite DB + log (tự tạo khi chạy)
├── discovery/
│   ├── google_search.py       ← Tìm công ty qua Google Custom Search API
│   └── hunter.py              ← Tìm email qua Hunter.io API
├── crawler/
│   └── katana_crawler.py      ← Wrap binary katana để crawl & trích email
├── email_sender/
│   ├── sender.py              ← Gửi email HTML qua SMTP
│   └── templates/
│       └── default.html       ← Template email (Jinja2)
└── db/
    └── database.py            ← SQLite — lưu trạng thái, tránh gửi trùng
```

## Luồng hoạt động

```
Google Custom Search API
        │
        ▼
  Tìm domain công ty  ──→  SQLite DB (companies)
        │
        ▼
Hunter.io API + Katana Crawler
        │
        ▼
  Tìm email theo domain  ──→  SQLite DB (emails)
        │
        ▼
  Gmail SMTP  ──→  Gửi email HTML  ──→  SQLite DB (send_log)
```

## Cài đặt

```bash
cd outreach
pip install -r requirements.txt
```

## Cấu hình

Chỉnh sửa `config.yaml`:

### 1. Google Custom Search API
- Tạo API key tại: https://console.developers.google.com
- Tạo Custom Search Engine tại: https://programmablesearchengine.google.com
- Điền `google_api_key` và `google_cx` vào config

### 2. Hunter.io API (tìm email theo domain)
- Đăng ký miễn phí tại: https://hunter.io
- Điền `hunter_api_key` vào config
- Gói free: 25 request/tháng; gói trả phí lên đến hàng nghìn

### 3. Gmail SMTP
- Bật 2-step verification trên tài khoản Google
- Tạo **App Password** tại: https://myaccount.google.com/apppasswords
- Điền email, app password vào config (KHÔNG dùng mật khẩu thông thường)

### 4. Nội dung email
- Chỉnh file `email_sender/templates/default.html`
- Điền thông tin của bạn trong section `email_template` của config

## Chạy

```bash
cd outreach

# Chạy toàn bộ pipeline (tìm → crawl → gửi mail)
python main.py

# Chỉ tìm công ty và email, chưa gửi
python main.py --discover-only

# Chỉ gửi email (từ DB đã có)
python main.py --send-only

# Import danh sách thủ công từ CSV rồi gửi
python main.py --import-csv companies.csv --send-only

# Xem thống kê
python main.py --stats
```

## Import CSV thủ công

Tạo file CSV với các cột (cột `domain` bắt buộc):

```csv
domain,name,url,email,first_name,last_name,position
stripe.com,Stripe,https://stripe.com,contact@stripe.com,John,Smith,CTO
shopify.com,Shopify,https://shopify.com,,,, 
```

```bash
python main.py --import-csv my_companies.csv
python main.py --send-only
```

## Tùy chỉnh query tìm kiếm

Trong `config.yaml`, phần `search.queries`:

```yaml
search:
  queries:
    - "fintech startup europe contact email"
    - "saas company B2B contact us site:linkedin.com/company"
    - "e-commerce platform asia official website"
  pages_per_query: 5
```

## Lưu ý pháp lý

- Đảm bảo tuân thủ **CAN-SPAM Act** (Mỹ) và **GDPR** (EU)
- Luôn bao gồm nội dung opt-out trong email (đã có sẵn trong template)
- Không gửi hàng loạt quá nhanh — đặt `delay_between_emails >= 5` giây
- Gmail giới hạn ~500 email/ngày với tài khoản thông thường
