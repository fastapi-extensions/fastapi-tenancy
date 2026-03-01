#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_and_test.sh — Start test infrastructure, run full test suite
# Usage:  bash setup_and_test.sh [pytest-extra-args...]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"

echo "════════════════════════════════════════════════════════════════"
echo "  fastapi-tenancy — Integration Test Runner"
echo "════════════════════════════════════════════════════════════════"

# ─── 1. Install dependencies ──────────────────────────────────────
echo ""
echo "▶  Installing dependencies (uv sync --all-extras --python 3.12)..."
cd "${PROJECT_DIR}"
uv sync --all-extras --python 3.12

# ─── 2. Start docker-compose services ────────────────────────────
echo ""
echo "▶  Starting docker-compose.test.yml services..."
docker compose -f docker-compose.test.yml up -d

# ─── 3. Wait for services to be healthy ──────────────────────────
echo ""
echo "▶  Waiting for services to become healthy..."

wait_healthy() {
  local container="$1"
  local max_wait=60
  local elapsed=0
  while [ $elapsed -lt $max_wait ]; do
    status=$(docker inspect --format='{{.State.Health.Status}}' "${container}" 2>/dev/null || echo "starting")
    if [ "$status" = "healthy" ]; then
      echo "    ✔  ${container} is healthy"
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
    echo "    ⏳ Waiting for ${container} (${elapsed}s)..."
  done
  echo "    ✘  ${container} did not become healthy in ${max_wait}s" >&2
  return 1
}

wait_healthy ft_test_postgres || true
wait_healthy ft_test_redis    || true
# MSSQL takes longer — mark as optional
wait_healthy ft_test_mssql   || echo "    ⚠  MSSQL may still be starting (it's slow)"
wait_healthy ft_test_mysql   || echo "    ⚠  MySQL may still be starting"

# ─── 4. Run the test suite ────────────────────────────────────────
echo ""
echo "▶  Running pytest..."
echo ""

export POSTGRES_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/test_tenancy"
export REDIS_URL="redis://localhost:6379/0"
export MYSQL_URL="mysql+aiomysql://root:root@localhost:3306/test_tenancy"
export MSSQL_URL="mssql+aioodbc://sa:YourStr0ng!Pass@localhost:1433/master?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"

uv run pytest \
  --cov \
  --cov-report=term-missing \
  -v \
  "$@" \
  || EXIT_CODE=$?

# ─── 5. Optionally tear down ──────────────────────────────────────
echo ""
echo "ℹ  Services left running. To stop them:"
echo "   docker compose -f docker-compose.test.yml down -v"
echo ""

exit "${EXIT_CODE:-0}"
