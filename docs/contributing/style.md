---
title: Code Style
description: Linting, type checking, security scanning, and commit message conventions for fastapi-tenancy.
---

# Code Style

## Automated tools

All style rules are enforced by CI and run with `make check`:

| Tool | Purpose | Config |
|------|---------|--------|
| `ruff check` | Linting (pyflakes, pycodestyle, isort, bugbear, …) | `pyproject.toml [tool.ruff]` |
| `ruff format` | Code formatting | `pyproject.toml [tool.ruff.format]` |
| `mypy --strict` | Type checking | `pyproject.toml [tool.mypy]` |
| `bandit` | Security SAST | tox `security` env |

```bash
make fmt   # auto-format with ruff
make lint  # lint without fixing
make type  # mypy strict
make check # all three
```

## Key conventions

- **Line length**: 100 characters
- **Quotes**: Double quotes (ruff format default)
- **Docstrings**: Google style (`Args:`, `Returns:`, `Raises:`)
- **Type annotations**: Full `Annotated[...]` style; no bare `Any` in public API
- **Imports**: `TYPE_CHECKING` guard for types only used in annotations

## Commit messages

```
type(scope): short description

Longer explanation if needed.

Fixes #123
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`, `perf`

Examples:

```
feat(isolation): add HybridIsolationProvider
fix(middleware): use reset(token) instead of clear() in finally block
docs(guides): add JWT resolution guide
test(storage): add bulk_update_status integration tests
```

## Pull request checklist

- [ ] `make check` passes (lint + type + security)
- [ ] `make test` passes (unit tests)
- [ ] New code has docstrings (Google style)
- [ ] Public API changes are reflected in `docs/`
- [ ] `CHANGELOG.md` updated under `[Unreleased]`
- [ ] Coverage stays ≥ 95%
