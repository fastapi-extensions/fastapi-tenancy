---
title: Running Tests
description: How to run the full test suite for fastapi-tenancy.
---

# Running Tests

## Quick commands

| Command | Description |
|---------|-------------|
| `make test` | Unit tests only — SQLite + in-memory, no Docker |
| `make test-int` | Starts Docker, runs integration tests |
| `make test-e2e` | Starts Docker, runs end-to-end tests |
| `make test-all` | All three suites |
| `make coverage` | Full suite + HTML coverage report (≥95% required) |

## Running specific tests

```bash
# A single file
pytest tests/unit/test_manager.py

# A specific test
pytest tests/unit/test_manager.py::TestTenancyManager::test_register_tenant

# By marker
pytest -m unit
pytest -m integration
pytest -m "not slow"
```

## Coverage

The project enforces ≥95% branch coverage. The coverage gate runs in CI and locally with `make coverage`.

```bash
make coverage
# Opens htmlcov/index.html in your browser
```

## Tox

Use `tox` to test against all supported Python versions:

```bash
pip install tox
tox -e py311    # unit tests on Python 3.11
tox -e lint     # ruff
tox -e type     # mypy --strict
tox -e coverage # full suite with coverage gate
```
