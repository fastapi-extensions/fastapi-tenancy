##########################################################################
# Makefile — fastapi-tenancy developer workflow							 #
#------------------------------------------------------------------------#
#																		 #
# Quick reference														 #
#------------------------------------------------------------------------#
#   make dev            Install package in editable mode with all extras #
#   make lint           ruff check + ruff format						 #
#   make fmt            Auto-fix formatting with ruff					 #
#   make type           mypy --strict									 #
#   make security       bandit SAST scan								 #
#   make check          lint + type + security							 #
#																		 #
#   make test           Unit tests only									 #
#   make test-int       Integration tests (starts Docker automatically)  #
#   make test-e2e       End-to-end tests (starts Docker automatically)   #
#   make test-all       Full suite (starts Docker automatically)		 #
#   make coverage       Full suite → HTML report in htmlcov/			 #
#																		 #
#   make docker-up      Start PostgreSQL 16 + MySQL 8 + Redis 7			 #
#   make docker-down    Tear down all test containers + volumes			 #
#																		 #
#   make build          Build wheel + sdist								 #
#   make clean          Remove all build / test artefacts				 #
##########################################################################

.PHONY: dev lint fmt type security check \
        test test-int test-e2e test-all coverage \
        docker-up docker-down \
        build clean

##########
# Config #
##########
COMPOSE_FILE  := docker-compose.test.yml
PYTEST        := python -m pytest
COV_FLAGS     := --cov=fastapi_tenancy \
                 --cov-report=term-missing \
                 --cov-report=html:htmlcov \
                 --cov-report=xml:coverage.xml
WAIT_SECS     := 20
PYTHON_VER	  := 3.12


#####################
# Development setup #
#####################
dev:
	uv sync --all-extras --python $(PYTHON_VER)

###################
# Static analysis #
###################
lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:
	uv run ruff check --fix src tests
	uv run ruff format src tests

type:
	uv run mypy src/fastapi_tenancy

security:
	uv run bandit -r src/fastapi_tenancy -ll -ii

check: lint type security

##########
# Docker #
##########
docker-up:
	@echo "Checking if test services are running..."
	@if [ -z "$$(docker compose -f $(COMPOSE_FILE) ps -q)" ]; then \
		echo "Services not running. Starting them..."; \
		docker compose -f $(COMPOSE_FILE) up -d; \
		echo "Waiting $(WAIT_SECS)s for services to become healthy…"; \
		sleep $(WAIT_SECS); \
	else \
		echo "Services already running. Skipping startup."; \
	fi
	@docker compose -f $(COMPOSE_FILE) ps
	@echo
	@echo

docker-down:
	docker compose -f $(COMPOSE_FILE) down -v --remove-orphans

#########
# Tests #
#########
test:
	uv run $(PYTEST) tests/unit/ --tb=short -v

test-int: docker-up
	uv run $(PYTEST) tests/integration/ --tb=short -v $(COV_FLAGS)

test-e2e: docker-up
	uv run $(PYTEST) tests/e2e/ --tb=short -v $(COV_FLAGS)

test-all: docker-up
	uv run $(PYTEST) tests/ --tb=short -v $(COV_FLAGS) 2>&1 | tee test-results.txt

coverage: docker-up
	uv run $(PYTEST) tests/ --tb=short $(COV_FLAGS)
	@echo ""
	@echo "HTML report → htmlcov/index.html"

#########
# Build #
#########
build:
	uv build

################
# Housekeeping #
################
clean:
	rm -rf dist build htmlcov coverage.xml test-results.txt .coverage
	rm -rf .pytest_cache .mypy_cache .ruff_cache .tox
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# MSSQL-specific test target
test-mssql: docker-up
	uv run $(PYTEST) tests/integration/test_mssql_store.py tests/integration/test_isolation_mssql.py --tb=short -v $(COV_FLAGS)
