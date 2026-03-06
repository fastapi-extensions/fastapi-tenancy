# Contributing to fastapi-tenancy

Thank you for taking the time to contribute! This document covers everything you need to get started.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Running Tests](#running-tests)
- [Code Style](#code-style)
- [Submitting Changes](#submitting-changes)
- [Reporting Bugs](#reporting-bugs)
- [Requesting Features](#requesting-features)

---

## Code of Conduct

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/) Code of Conduct. Be respectful and constructive in all interactions.

---

## Getting Started

### Prerequisites

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) — fast Python package manager

### Clone & Install

```bash
git clone https://github.com/fastapi-extensions/fastapi-tenancy
cd fastapi-tenancy
uv sync --all-extras
```

This installs all runtime dependencies plus every dev/test extra into a managed virtual environment.

---

## Development Workflow

```bash
# Activate the venv (optional — uv run works without it)
source .venv/bin/activate

# Run the full test suite
uv run pytest --cov -v

# Run linter
uv run ruff check src tests

# Auto-fix lint issues
uv run ruff check --fix src tests

# Format code
uv run ruff format src tests

# Type-check
uv run mypy src
```

---

## Running Tests

Tests are organised into three groups controlled by pytest markers:

| Marker | Description | Requires |
|---|---|---|
| `unit` | Pure unit tests, no I/O | nothing |
| `integration` | SQLite-backed or mocked tests | nothing |
| `e2e` | Live database tests | running PostgreSQL / Redis |

```bash
# Unit + integration only (CI default)
uv run pytest -m "unit or integration" --cov

# All tests including e2e (needs docker-compose)
docker compose -f docker-compose.test.yml up -d
uv run pytest --cov -v
```

When adding a new test, pick the correct marker and place the file in the appropriate subdirectory under `tests/`.

---

## Code Style

- **Formatter / linter**: `ruff` (config in `pyproject.toml`)
- **Type checker**: `mypy` strict mode
- **Docstrings**: Google style (`pydocstyle` convention)
- **Line length**: 100 characters

All of these are enforced in CI. Run `uv run ruff format src tests && uv run ruff check src tests` before pushing.

---

## Submitting Changes

1. **Fork** the repository and create a branch from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```

2. **Write tests** for your change. New features must include unit tests; bug fixes must include a regression test.

3. **Ensure all checks pass**:
   ```bash
   uv run pytest --cov -v
   uv run ruff check src tests
   uv run mypy src
   ```

4. **Update `CHANGELOG.md`** under the `[Unreleased]` section, following the existing format.

5. **Open a Pull Request** against `main`. Fill in the PR template, link any relevant issues, and describe the change clearly.

### Commit Message Format

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add JWT resolver support for custom claims
fix: handle missing Host header in SubdomainTenantResolver
docs: add schema isolation quick-start example
test: add regression for corrupted metadata recovery
chore: bump asyncpg to 0.30.0
```

---

## Reporting Bugs

Open an issue at [GitHub Issues](https://github.com/fastapi-extensions/fastapi-tenancy/issues) and include:

- Your `fastapi-tenancy` version (`python -c "import fastapi_tenancy; print(fastapi_tenancy.__version__)"`)
- Python version and OS
- Minimal reproducible example
- Full traceback

---

## Requesting Features

Open a [GitHub Discussion](https://github.com/fastapi-extensions/fastapi-tenancy/discussions) or issue labelled `enhancement`. Describe the use case, not just the solution, so we can find the best approach together.
