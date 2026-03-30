#!/usr/bin/env bash
# ============================================================
# crawl-company.sh — Crawl toàn bộ thông tin công ty
# Đặc biệt tập trung vào email liên hệ
#
# Cách dùng:
#   chmod +x crawl-company.sh
#   ./crawl-company.sh https://example.com
#   ./crawl-company.sh https://example.com --deep
#   ./crawl-company.sh https://example.com --headless
#   ./crawl-company.sh https://example.com --deep --headless
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KATANA="$SCRIPT_DIR/katana"
FIELD_CONFIG="$SCRIPT_DIR/field-config.yaml"

# ── Kiểm tra tham số ────────────────────────────────────────
if [[ $# -lt 1 ]]; then
  echo "Cách dùng: $0 <URL> [--deep] [--headless] [--depth N] [--rate-limit N]"
  echo "  Ví dụ: $0 https://company.com"
  echo "  Ví dụ: $0 https://company.com --deep --headless"
  exit 1
fi

TARGET_URL="$1"; shift

# ── Tuỳ chọn mặc định ───────────────────────────────────────
DEPTH=4
HEADLESS=false
RATE_LIMIT=50
DELAY=0
CONCURRENCY=10
EXTRA_FLAGS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deep)       DEPTH=6; RATE_LIMIT=30; shift ;;
    --headless)   HEADLESS=true; EXTRA_FLAGS="$EXTRA_FLAGS -headless -system-chrome -no-sandbox -xhr"; shift ;;
    --depth)      DEPTH="$2"; shift 2 ;;
    --rate-limit) RATE_LIMIT="$2"; shift 2 ;;
    --delay)      DELAY="$2"; shift 2 ;;
    *) echo "[WARN] Bỏ qua tham số không xác định: $1"; shift ;;
  esac
done

# ── Thư mục output ──────────────────────────────────────────
DOMAIN=$(echo "$TARGET_URL" | sed 's|https\?://||;s|/.*||;s|:.*||')
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$SCRIPT_DIR/crawl_output/${DOMAIN}_${TIMESTAMP}"
mkdir -p "$OUTPUT_DIR/fields"

echo "════════════════════════════════════════════════════════"
echo "  KATANA — Crawl thông tin công ty"
echo "  Target   : $TARGET_URL"
echo "  Depth    : $DEPTH"
echo "  Headless : $HEADLESS"
echo "  Output   : $OUTPUT_DIR"
echo "════════════════════════════════════════════════════════"
echo ""

# ── Bước 1: Crawl toàn bộ, extract fields liên hệ ──────────
echo "[1/2] Crawl + extract fields liên hệ (email, phone, mạng xã hội)..."

$KATANA \
  -u "$TARGET_URL" \
  -d "$DEPTH" \
  -fs rdn \
  -jc \
  -jsl \
  -kf "all" \
  -aff \
  -c "$CONCURRENCY" \
  -rl "$RATE_LIMIT" \
  -rd "$DELAY" \
  -timeout 20 \
  -retry 2 \
  -flc "$FIELD_CONFIG" \
  -sf "email,phone,linkedin,facebook,url" \
  -sfd "$OUTPUT_DIR/fields" \
  -j \
  -o "$OUTPUT_DIR/crawl.jsonl" \
  -silent \
  $EXTRA_FLAGS \
  2>"$OUTPUT_DIR/error.log" || true

echo "[1/2] Xong."

# ── Bước 2: Post-process ────────────────────────────────────
echo "[2/2] Trích xuất và làm sạch danh sách email..."

EMAIL_REGEX='[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'

# Tổng hợp từ file field được lưu
if ls "$OUTPUT_DIR/fields/"*_email.txt &>/dev/null; then
  cat "$OUTPUT_DIR/fields/"*_email.txt 2>/dev/null \
    | grep -oP "$EMAIL_REGEX" \
    | sort -u > "$OUTPUT_DIR/emails_raw.txt"
else
  touch "$OUTPUT_DIR/emails_raw.txt"
fi

# Bổ sung từ toàn bộ JSONL (body, response)
grep -oP "$EMAIL_REGEX" "$OUTPUT_DIR/crawl.jsonl" 2>/dev/null \
  | sort -u >> "$OUTPUT_DIR/emails_raw.txt" || true

# Lọc bỏ email giả/thư viện JS
grep -iP "@[a-z0-9.\-]+\.[a-z]{2,}$" "$OUTPUT_DIR/emails_raw.txt" 2>/dev/null \
  | grep -ivP "@(example|test|domain|yourdomain|email|schematics|types|angular|babel|jest|rollup|webpack|node_modules)" \
  | grep -ivP "\.(png|jpg|gif|css|js|ts|svg|woff|ttf|ico)$" \
  | sort -u > "$OUTPUT_DIR/emails_final.txt" || true

# Trích URL đã crawl
grep -oP '"endpoint":"[^"]+"' "$OUTPUT_DIR/crawl.jsonl" 2>/dev/null \
  | sed 's/"endpoint":"//;s/"//' \
  | sort -u > "$OUTPUT_DIR/all_urls.txt" || true

echo "[2/2] Xong."

# ── Tóm tắt ─────────────────────────────────────────────────
URL_COUNT=$(wc -l < "$OUTPUT_DIR/all_urls.txt" 2>/dev/null || echo 0)
EMAIL_COUNT=$(wc -l < "$OUTPUT_DIR/emails_final.txt" 2>/dev/null || echo 0)

echo ""
echo "════════════════════════════════════════════════════════"
echo "  KẾT QUẢ"
echo "════════════════════════════════════════════════════════"
echo "  URLs   : $URL_COUNT"
echo "  Emails : $EMAIL_COUNT"
echo ""

if [[ $EMAIL_COUNT -gt 0 ]]; then
  echo "  DANH SÁCH EMAIL TÌM ĐƯỢC:"
  echo "  ──────────────────────────"
  cat "$OUTPUT_DIR/emails_final.txt" | sed 's/^/  /'
else
  echo "  [!] Không tìm thấy email trực tiếp."
  echo "  Gợi ý: thêm --headless để render JavaScript,"
  echo "         hoặc tăng depth: --depth 6"
fi

echo ""
echo "  Files đầu ra:"
echo "  ├── crawl.jsonl        — Toàn bộ dữ liệu crawl (JSONL)"
echo "  ├── all_urls.txt       — Danh sách URL"
echo "  ├── emails_final.txt   — Email (đã lọc & dedup)"
echo "  ├── emails_raw.txt     — Email thô (chưa lọc)"
echo "  ├── fields/            — Dữ liệu field theo từng domain"
echo "  └── error.log          — Log lỗi"
echo "  Thư mục: $OUTPUT_DIR"
echo "════════════════════════════════════════════════════════"
