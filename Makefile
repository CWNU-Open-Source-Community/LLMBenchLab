SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help setup dev dev-multi backend worker frontend test lint format smoke migrate phase2-acceptance phase2-capacity phase2-slo docker-up docker-down

help:
	@echo "LLMBenchLab developer commands:"
	@echo "  make setup        Install dependencies and initialize the local database"
	@echo "  make dev          Start local API, Worker(s), and frontend (DEV_WORKERS=N needs PostgreSQL)"
	@echo "  make dev-multi    Start PostgreSQL/Redis/API/frontend with two Workers (WORKERS=N)"
	@echo "  make backend      Start only the FastAPI development server"
	@echo "  make worker       Start only the independent task Worker"
	@echo "  make frontend     Start only the Vite development server"
	@echo "  make test         Run backend and frontend test suites"
	@echo "  make lint         Run backend lint/format checks and frontend lint/typecheck"
	@echo "  make format       Apply safe automatic formatting fixes"
	@echo "  make smoke        Run the fully offline Mock vertical-slice smoke test"
	@echo "  make migrate      Apply all Alembic migrations"
	@echo "  make phase2-acceptance  Run isolated real-Compose reliability fault tests"
	@echo "  make phase2-capacity    Run bounded two-Worker Mock capacity baseline"
	@echo "  make phase2-slo         Run the multi-trial single-host Mock qualification"
	@echo "  make docker-up    Build and start the Compose stack"
	@echo "  make docker-down  Stop the Compose stack"

setup:
	@./scripts/setup.sh

dev:
	@if [[ "$(origin DEV_WORKERS)" != "undefined" ]]; then \
		LLMBENCHLAB_DEV_WORKER_PROCESSES="$(DEV_WORKERS)" ./scripts/dev.sh; \
	else \
		./scripts/dev.sh; \
	fi

dev-multi: docker-up

backend:
	@./scripts/bootstrap_credential_keyring.sh
	@set -a; \
	if [[ -f .env ]]; then source ./.env; fi; \
	set +a; \
	cd backend && uv run uvicorn app.main:app \
		--host "$${API_HOST:-127.0.0.1}" \
		--port "$${API_PORT:-8000}" \
		--reload

worker:
	@./scripts/bootstrap_credential_keyring.sh
	@set -a; \
	if [[ -f .env ]]; then source ./.env; fi; \
	set +a; \
	cd backend && uv run python -m app.worker

frontend:
	@set -a; \
	if [[ -f .env ]]; then source ./.env; fi; \
	set +a; \
	cd frontend && npm run dev -- --host "$${FRONTEND_HOST:-127.0.0.1}"

test:
	@cd backend && uv run pytest
	@cd frontend && npm test

lint:
	@cd backend && uv run ruff check .
	@cd backend && uv run ruff format --check .
	@cd frontend && npm run lint
	@cd frontend && npm run typecheck

format:
	@cd backend && uv run ruff check . --fix
	@cd backend && uv run ruff format .
	@cd frontend && npm run lint -- --fix

smoke:
	@./scripts/smoke.sh

migrate:
	@./scripts/migrate.sh

phase2-acceptance:
	@python3 scripts/phase2_acceptance.py

phase2-capacity:
	@python3 scripts/phase2_capacity.py

phase2-slo:
	@python3 -I scripts/phase2_slo.py

docker-up:
	@./scripts/bootstrap_credential_keyring.sh
	@if [[ "$(origin WORKERS)" != "undefined" ]]; then \
		LLMBENCHLAB_COMPOSE_WORKER_PROCESSES="$(WORKERS)" ./scripts/compose_up.sh; \
	else \
		./scripts/compose_up.sh; \
	fi

docker-down:
	@docker compose down --remove-orphans
