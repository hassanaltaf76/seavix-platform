# SeaVix dev runner — start only what you're working on. Never all 7 services.

.PHONY: infra up down test

infra:            ## Start shared infra (Postgres + OpenFGA + Firebase emulator)
	docker compose up -d
	@echo "Postgres   : postgres://seavix:dev@localhost:5432"
	@echo "OpenFGA    : http://localhost:8080"
	@echo "Firebase UI: http://localhost:4000  (create test users here)"

down:
	docker compose down

# Run services as plain processes, e.g.:  make dev SERVICES="identity work"
dev:
	@for s in $(SERVICES); do \
	  (cd services/$$s && uv run uvicorn app.main:app --port $$PORT --reload) & \
	done; wait
# PORT per service lives in services/<name>/.env

test:
	@pytest packages services -q
