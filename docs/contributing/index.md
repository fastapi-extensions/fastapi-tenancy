---
title: Contributing
description: How to contribute to fastapi-tenancy — setup, style, testing, and pull request process.
---

# Contributing

Thank you for considering a contribution to fastapi-tenancy! This section covers everything you need to go from zero to opening a pull request.

- [Development Setup](setup.md) — install the project in editable mode, configure pre-commit hooks
- [Running Tests](testing.md) — unit, integration, and end-to-end test commands
- [Code Style](style.md) — ruff, mypy, bandit, and commit message conventions

## Quick start

```bash
git clone https://github.com/fastapi-extensions/fastapi-tenancy.git
cd fastapi-tenancy
make dev   # installs all extras in editable mode
make check # runs lint, type-check, and security scan
make test  # runs unit tests (no Docker needed)
```
