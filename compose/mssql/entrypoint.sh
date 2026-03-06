#!/usr/bin/env bash

#################################################################################
# mssql/entrypoint.sh                                                           #
#                                                                               #
# Entrypoint for the custom SQL Server test image.                              #
#                                                                               #
# Sequence:                                                                     #
#   1. Start sqlservr in the background (original PID 1 will be this script).   #
#   2. Poll /opt/mssql-tools18/bin/sqlcmd until the engine accepts connections  #
#      (up to MAX_WAIT_SEC seconds; default 120).                               #
#   3. Run idempotent T-SQL to create test_db with a UTF-8 collation.           #
#   4. exec-wait on sqlservr so it becomes PID 1 and receives SIGTERM on        #
#      `docker stop`.                                                           #
#                                                                               #
# Environment variables consumed:                                               #
#   SA_PASSWORD      — SA password set by docker-compose (required).            #
#   MSSQL_DB         — Database name to create (default: test_db).              #
#   MAX_WAIT_SEC     — Maximum seconds to wait for engine ready (default: 120). #
#################################################################################
set -euo pipefail

SA_PASSWORD="${SA_PASSWORD:?SA_PASSWORD must be set}"
MSSQL_DB="${MSSQL_DB:-test_db}"
MAX_WAIT_SEC="${MAX_WAIT_SEC:-120}"
SQLCMD="/opt/mssql-tools18/bin/sqlcmd"

echo "[entrypoint] Starting SQL Server engine in background..."
/opt/mssql/bin/sqlservr &
SQLSERVR_PID=$!

echo "[entrypoint] Waiting for SQL Server to accept connections (max ${MAX_WAIT_SEC}s)..."
waited=0
until "${SQLCMD}" -S localhost -U sa -P "${SA_PASSWORD}" -C -Q "SELECT 1" >/dev/null 2>&1; do
    if (( waited >= MAX_WAIT_SEC )); then
        echo "[entrypoint] ERROR: SQL Server did not become ready within ${MAX_WAIT_SEC}s" >&2
        kill "${SQLSERVR_PID}" 2>/dev/null || true
        exit 1
    fi
    echo "[entrypoint] Attempt ${waited}/${MAX_WAIT_SEC}: engine not ready yet, sleeping 2s..."
    sleep 2
    (( waited += 2 ))
done

echo "[entrypoint] SQL Server is ready. Creating database '${MSSQL_DB}'..."
"${SQLCMD}" -S localhost -U sa -P "${SA_PASSWORD}" -C -Q "
IF NOT EXISTS (
    SELECT name FROM sys.databases WHERE name = N'${MSSQL_DB}'
)
BEGIN
    CREATE DATABASE [${MSSQL_DB}]
    COLLATE Latin1_General_100_CI_AS_SC_UTF8;
    PRINT 'Database ${MSSQL_DB} created.';
END
ELSE
BEGIN
    PRINT 'Database ${MSSQL_DB} already exists.';
END

-- Minimise log file growth during tests
ALTER DATABASE [${MSSQL_DB}] SET RECOVERY SIMPLE;
-- Allow snapshot isolation for concurrent test transactions
ALTER DATABASE [${MSSQL_DB}] SET ALLOW_SNAPSHOT_ISOLATION ON;
ALTER DATABASE [${MSSQL_DB}] SET READ_COMMITTED_SNAPSHOT ON;
" 2>&1

echo "[entrypoint] Initialization complete. Handing off to sqlservr (PID ${SQLSERVR_PID})..."
# Wait on the background sqlservr so this script stays as the process group
# leader and forwards signals correctly.
wait "${SQLSERVR_PID}"
