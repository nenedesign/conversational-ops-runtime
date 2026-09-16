.PHONY: install db-up db-down dev test

install:
	pip3 install --require-hashes -r requirements.txt
	pip3 install --require-hashes -r requirements-audit.txt
	pip-audit -r requirements.txt
	@echo "All packages verified — no known CVEs found."

db-up:
	docker compose up -d postgres
	@echo "Waiting for Postgres..."
	@until docker compose exec -T postgres pg_isready -U cor -d cor_dev 2>/dev/null; do sleep 1; done
	@docker compose exec -T postgres psql -U cor -d cor_dev -c "SELECT 1 FROM tenants LIMIT 1" >/dev/null 2>&1 || cat migrations/001_initial.sql | docker compose exec -T postgres psql -U cor -d cor_dev
	@echo "Postgres ready."

db-down:
	docker compose down

db-reset:
	docker compose down -v
	docker compose up -d postgres

dev:
	cp -n .env.example .env 2>/dev/null || true
	python3 -m uvicorn src.main:app --reload --port 8080

test:
	pytest conformance/ -v
