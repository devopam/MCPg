---
title: Documentation
---

# MCPg documentation

A landing page for the published docs. Items are grouped roughly by
"first read this", "then this", "and reach for these when you need
them" — pick the section that matches what you're trying to do.

## Get started

- [**Installation**](installation.md) — `pip install mcpg`, the
  Docker image, and what the environment variables mean.
- [**Tour**](tour.md) — a guided walkthrough of every MCP tool MCPg
  ships, grouped by capability area.
- [**Cookbook**](cookbook.md) — short, copy-pasteable recipes for
  the common workflows (read-replica routing, NL→SQL,
  OIDC bearer auth, RLS, hybrid search, …).

## Reference

- [**Tools**](tools.md) — every MCP tool MCPg exposes, including
  the capability gates that need to be on.
- [**Architecture**](architecture.md) — how the pieces fit together
  (server, drivers, replicas, cursors, audit, transports).
- [**Scaling guide**](scaling.md) — pool sizing, replica fan-out,
  observability, performance tuning notes.

## Operate

- [**Security hardening roadmap**](security-hardening.md) — shipped
  vs queued security features.
- [**Release process**](release-process.md) — the playbook for
  cutting a new MCPg release to PyPI.
- [**User guide**](user-guide.md) — end-user-facing reference.

## Project history

- [**Comparison matrix**](comparison-matrix.md) — MCPg against
  other PostgreSQL MCP servers.
- [**Feature roadmap**](feature-shortlist.md) — what's planned,
  and the persistent home for gaps surfaced during PR / phase
  retrospectives.
- [**Progress log**](PROGRESS.md) — chronological build log.
- Release notes:
  [v0.6.0](release-notes-0.6.0.md) ·
  [v0.5.0](release-notes-0.5.0.md) ·
  [v0.4.0](release-notes-0.4.0.md) ·
  [v0.3.0](release-notes-0.3.0.md) ·
  see [CHANGELOG](../CHANGELOG.md) for patch releases and unreleased work.
- [**Architecture Decision Records**](adr/) — the durable design
  decisions, one ADR per choice.

---

For the project source, issue tracker, and contribution guidelines,
head to [github.com/devopam/MCPg](https://github.com/devopam/MCPg).
