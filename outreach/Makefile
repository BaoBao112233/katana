"""
outreach/Makefile
Quick commands to run the system.

  make up           — start full stack (docker compose)
  make dev          — start in dev mode (with Kibana)
  make worker       — start all Celery workers locally
  make api          — start FastAPI locally
  make migrate      — run Alembic migrations
  make collect      — trigger one-shot Google Maps collection
  make install      — install Python deps + Playwright
  make test         — run tests
"""

.PHONY: up dev down worker api migrate collect install test shell-db logs

# ── Infrastructure ──────────────────────────────────────────────────────────

up:
	docker compose up -d postgres redis elasticsearch api worker-crawl worker-ai worker-enrich worker-collect beat flower

dev:
	docker compose --profile dev up -d

down:
	docker compose down

logs:
	docker compose logs -f

# ── Local development ───────────────────────────────────────────────────────

install:
	pip install -r requirements.txt
	playwright install chromium
	playwright install-deps chromium

api:
	uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

worker:
	celery -A workers.celery_app worker -Q crawl,ai,enrich,collect --concurrency=4 --loglevel=info

beat:
	celery -A workers.celery_app beat --loglevel=info

flower:
	celery -A workers.celery_app flower --port=5555

# ── Database ────────────────────────────────────────────────────────────────

migrate:
	alembic upgrade head

migrate-auto:
	alembic revision --autogenerate -m "auto"

shell-db:
	docker compose exec postgres psql -U postgres -d companydb

# ── Data collection ─────────────────────────────────────────────────────────

collect:
	python -c "from workers.tasks import task_collect_google_maps; task_collect_google_maps.delay(['software company', 'fintech startup'], '').get(timeout=300)"

collect-cc:
	python -c "from workers.tasks import task_collect_common_crawl; task_collect_common_crawl.delay(['com', 'io'], 10000).get(timeout=600)"

dispatch:
	python -c "from workers.tasks import task_dispatch_pending_domains; task_dispatch_pending_domains.delay(500).get(timeout=60)"

# ── Testing ─────────────────────────────────────────────────────────────────

test:
	pytest tests/ -v

test-ai:
	pytest tests/test_ai.py -v

# ── API keys ─────────────────────────────────────────────────────────────────

create-api-key:
	python scripts/create_api_key.py

# ── Stats ───────────────────────────────────────────────────────────────────

stats:
	curl -s http://localhost:8000/stats | python -m json.tool
