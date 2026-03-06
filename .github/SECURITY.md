# Security Policy

## Supported Versions

| Version | Supported       |
|---------|-----------------|
| 0.2.x   | ✅ Active        |
| < 0.2   | ❌ Not supported |

## Reporting a Vulnerability

**Please do NOT open a public GitHub issue for security vulnerabilities.**

fastapi-tenancy handles multi-tenant data isolation. Security issues here
may affect data separation between tenants — always treated as critical.

### How to Report

1. **GitHub private vulnerability reporting** (preferred):  
   Go to **Security → Report a vulnerability** in this repository.

2. **Email**: Reach out through the contact listed in `pyproject.toml`.

### What to Include

- Description of the vulnerability
- Steps to reproduce
- Affected versions
- Any known mitigations

### Response Timeline

- **Initial response**: within 72 hours
- **Status update**: within 7 days
- **Fix timeline**: critical issues within 14 days

### Scope

Issues in scope:
- Cross-tenant data leakage
- SQL injection via tenant identifiers or metadata
- Authentication/authorisation bypass in middleware
- Rate-limit bypass
- Unsafe deserialization

Issues out of scope:
- Denial of service via resource exhaustion without auth bypass
- Issues requiring physical access to the server
