# Security Policy

## Supported versions

Only the latest minor release line receives security fixes. Currently:

| Version | Supported          |
|---------|--------------------|
| 0.5.x   | :white_check_mark: |
| 0.4.x   | :x:                |
| <0.4    | :x:                |

If you're running an older release, **upgrade first** — most security
fixes ride a minor-version bump.

## Reporting a vulnerability

**Please do not file public issues for security reports.**

Email `devopam@gmail.com` with:

- A description of the issue and its impact
- Steps to reproduce (proof-of-concept code is welcome)
- The MCPg version (`uv run mcpg --version` or `mcpg --version`)
- The PostgreSQL version (`SELECT version()`)
- Whether you've checked the issue against the latest trunk

You'll receive an acknowledgement within **3 business days**. Confirmed
issues get a CVE assignment where appropriate, a fix on a private
branch, and a coordinated release. Reporters are credited in the
release notes unless they prefer otherwise.

## Scope

**In scope:**

- The MCPg server code under `src/mcpg/` (excluding `src/mcpg/_vendor/`)
- Authentication & authorisation paths (bearer-token, OIDC,
  multi-tenancy `SET LOCAL ROLE`)
- The capability gates that restrict tool surfaces by access mode
- SQL injection paths in any tool MCPg ships
- Audit trail integrity and credential redaction
- Rate limiter bypasses

**Out of scope:**

- Vulnerabilities in PostgreSQL itself
- Vulnerabilities in the vendored SQL-safety kernel at
  `src/mcpg/_vendor/sql/` — those go upstream to
  `crystaldba/postgres-mcp`
- Issues that require an attacker already to have `unrestricted`
  access mode AND `MCPG_ALLOW_DDL=true` (that combination is
  by-design root access)
- Vulnerabilities in third-party Python dependencies — report
  directly to the upstream project; we'll bump when they patch
- Bugs that only affect feature branches not on `main`

## Disclosure timeline

We aim for a 90-day coordinated disclosure window from acknowledgement.
Critical vulnerabilities ship faster (typically within 14 days of
confirmation). The reporter is consulted on timing.
