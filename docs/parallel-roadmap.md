# MCPg parallel-work roadmap

A planning view of the **remaining** feature work, organised so several
contributors can pick items up **simultaneously in separate PRs** with
minimal merge friction.

- The full menu (scope/effort/value per item) lives in
  [`feature-shortlist.md`](feature-shortlist.md).
- Security-specific status lives in
  [`security-hardening.md`](security-hardening.md).
- What already shipped is in [`../CHANGELOG.md`](../CHANGELOG.md).

This doc adds the thing those don't: **a conflict map and a batching
plan** so parallel PRs don't step on each other.

---

## 1. Recently shipped (don't re-do)

The last wave landed these — context for what's already covered:

- Security hardening: HTTP headers / body limit / CORS / request
  timeout, audit-log HMAC integrity (`verify_audit_chain`), graceful
  shutdown, subprocess hardening (bin-allowlist + rlimits + temp cwd).
- Compliance: `verify_connection_encryption`, `prune_audit_events`.
- Secrets backend: `env` + `file` (`MCPG_SECRETS_BACKEND`).
- Advisors: redundant-index detector. pgvector: `analyze_hnsw_recall`,
  `mmr_search`.

---

## 2. Conflict map — the shared hot files

Almost every feature PR touches a small set of shared files. Knowing
the rules up front keeps parallel PRs to trivial, mechanical conflicts.

| File | Why it's shared | Rule to stay out of each other's way |
|---|---|---|
| `src/mcpg/tools.py` | Every new tool registers here via `@server.tool`. | Register inside the **existing family registrar** for your area (`_register_query`, `_register_liveops`, `_register_data_movement_*`, …). Keep the block self-contained. Conflicts are adjacent-block only — trivial to resolve; rebase before final push. |
| `src/mcpg/config.py` | Any new `MCPG_*` env var adds a `Settings` field + a parse block + a `__repr__` line + a constructor arg (4 spots). | Append your field at the **end of its logical group**, not mid-list. Touch only your 4 lines. |
| `CHANGELOG.md` | Every PR adds an `[Unreleased]` bullet. | Add your bullet at the **top of `### Added`**; resolve the inevitable top-of-file conflict by keeping both. |
| `docs/tour.md`, `docs/user-guide.md`, `docs/tools.md` | All three carry a hard-coded **tool count** (`NNN tools`). N parallel PRs each bumping it = guaranteed N-way conflict. | **Do NOT touch the tool count in a feature PR.** Add the tool's *line* to `tour.md`, but leave the count alone. A periodic "doc sync" PR reconciles the number from `grep -c '@server.tool' src/mcpg/tools.py`. |
| `docs/feature-shortlist.md` / `security-hardening.md` | Status flips (`✅`). | Flip only your row. |

**New-module bias.** Where an item is a cohesive cluster (e.g. vector
analytics, observability), prefer a **new module** over piling into an
existing one — it removes the file-level conflict entirely and only
leaves the `tools.py` registration + `config.py` touch points.

---

## 3. Workstreams (independent unless noted)

Each row is a candidate PR. "New module?" flags whether it lands mostly
in fresh files (low conflict) vs. edits a shared module. Effort scale
matches `feature-shortlist.md` (S / M / L).

### A. pgvector analytics — `import_vectors`, clustering, drift

> Recommendation: put the **analytics** tools in a new module
> `src/mcpg/vector_ops.py` rather than growing `textsearch.py` (which
> already holds search). `import_vectors` is closer to
> `data_movement.py`.

| PR | Item | New module? | Effort | Depends on | Notes |
|---|---|---|---|---|---|
| A1 | `import_vectors` (9.6) | extends `data_movement.py` | S | — | Sibling of `import_csv`; `vector(N)` dim validation; WRITE-gated. |
| A2 | `analyze_distance_metric` (9.8) | `vector_ops.py` (new) | S | — | Pure read + stats; recommends cosine/L2/IP from magnitude spread. |
| A3 | `cross_table_similarity` (9.7) | `vector_ops.py` | S | — | k-NN from one row in A against table B (same dim). |
| A4 | `monitor_index_build` (9.9) | `liveops.py` | S | — | Reads `pg_stat_progress_create_index`; lives by `list_active_queries`. |
| A5 | `cluster_vectors` (9.3) | `vector_ops.py` | M | — | k-means; centroids + per-row labels. Reuse `mmr_search`'s cosine helpers. |
| A6 | `detect_vector_outliers` (9.4) | `vector_ops.py` | S-M | A5 (shares centroid logic) | Flag rows far from any centroid. |
| A7 | `monitor_embedding_drift` (9.5) | `vector_ops.py` | M | — | Distributional stats over time windows. |
| A8 | `migrate_vector_to_halfvec` (9.10) | extends `vector_tuning.py` | S-M | — | DDL generator; uses the shadow-migration workflow; DDL-gated. |

A1–A4 are mutually independent and can all go at once. A5 then A6 are a
mini-sequence (shared centroid code).

### B. Observability

> All three live around a small new `mcpg/obs_logging.py` (or extend
> `mcpg/observability.py`) + a couple of `config.py` flags. Independent
> of every other workstream.

| PR | Item | New module? | Effort | Notes |
|---|---|---|---|---|
| B1 | Structured JSON logging toggle (1.2) | extends logging setup | S | `MCPG_LOG_FORMAT=text\|json`. Wraps the existing logger. |
| B2 (✅ PR) | Slow-call logging from the MCP layer (1.3) | middleware hook | S | Per-tool latency warn over a threshold. |
| B3 | OpenTelemetry spans per tool call (1.1) | new, optional extra | M | One span per `call_tool` + child spans; behind `mcpg[otel]`. |

B1/B2 are independent; B3 is larger and best alone.

### C. Security (remaining)

| PR | Item | New module? | Effort | Depends on | Notes |
|---|---|---|---|---|---|
| C1 | IP allowlist for HTTP transport (4.3) | `http_runtime.py` middleware | S | — | Tiny ASGI middleware + `MCPG_HTTP_IP_ALLOWLIST`. |
| C2 | mTLS for the HTTP transport (4.4) | `http_runtime.py` / `run_http` | S | — | Client-cert verification (uvicorn ssl params). |
| C3 | Secrets cloud backends — Vault / AWS / GCP | extends `secrets.py` | L | — | Each behind an extra (`mcpg[vault]` …); same `MCPG_SECRETS_BACKEND` switch. Can split per provider. |

C1 and C2 both edit `http_runtime.py`'s middleware stack — **sequence
them** (or coordinate) to avoid colliding on `build_http_app`.

### D. PostgreSQL feature coverage

| PR | Item | New module? | Effort | Notes |
|---|---|---|---|---|
| D1 | Logical replication writes (2.1) | new `replication.py` | M | `create/drop_publication`, `create/drop_subscription`; DDL-gated. |
| D2 (✅ PR) | `pg_buffercache` integration (2.2) | extends `io_stats.py` | S | Buffer-level cache analysis (needs the extension). |
| D3 | WAL inspection `pg_walinspect` (2.3) | new small module | S | Niche; replication debugging. |
| D4 (✅ PR #45) | Deadlock-cycle walker (2.4) | extends `locks.py` | S-M | Reconstruct cycles beyond `find_blocking_chains` pairs. |

All four are independent.

### E. Migration ecosystem

| PR | Item | New module? | Effort | Notes |
|---|---|---|---|---|
| E1 | Migration history table read (7.3) | new `migration_history.py` | S | Read Alembic / Flyway / Diesel bookkeeping tables. |
| E2 | Zero-downtime migration cookbook (7.4) | docs only | S | Pure `docs/cookbook.md` patterns — zero code, zero conflict. |
| E3 | Pre-deployment migration validation (7.2) | extends `migrations.py` | M | Composes `compare_schemas` + shadow workflow. |
| E4 | Alembic/Flyway/Liquibase ingestion (7.1) | extends `migrations.py` | M-L | Large; do after E3. |

### F. Developer experience / agent UX

| PR | Item | New module? | Effort | Notes |
|---|---|---|---|---|
| F1 (✅ PR #44) | Schema-doc generator (8.2) | new `schema_docs.py` | S | Markdown table reference from the catalog; sibling of `generate_schema_diagram`. |
| F2 | Auto tool examples in descriptions (3.1) | touches `tools.py` broadly | S | Higher conflict (edits many descriptions) — best done solo, late. |
| F3 | `seed_table_with_sample_data` (3.2) | extends `test_data.py` | M | Executes inserts (WRITE-gated); sibling of `generate_test_data`. |
| F4 | Test-data factory `generate_test_row_for` (8.1) | extends `test_data.py` | M | Catalog + heuristics. F3/F4 share `test_data.py` — sequence them. |

### G. Backups & DR (narrow audience — lower priority)

| PR | Item | Effort | Notes |
|---|---|---|---|
| G1 | Scheduled logical backups via `pg_cron` + `dump_database` (5.1) | S | Composes existing tools. |
| G2 | WAL archive inspection (5.2) | M | Niche. |
| G3 | Point-in-time recovery prep helpers (5.3) | M | Heavy lift, narrow audience. |

### Deferred

- Multi-database support (10.1, **L**) — one server, many DSNs. Big
  architectural change (pool-per-DB, per-tool selector, gate rework).
  No concrete demand; leave parked.

---

## 4. Suggested first parallel batch

Pick items that touch **disjoint modules** so they can land in any
order. A good opening wave of ~5 simultaneous PRs:

1. **A1** `import_vectors` — `data_movement.py`
2. **A2** `analyze_distance_metric` — new `vector_ops.py`
3. **B1** JSON logging toggle — logging setup + `config.py`
4. **D4** deadlock-cycle walker — `locks.py`
5. **F1** schema-doc generator — new `schema_docs.py`

These five share only `tools.py` (registration) + `config.py` (B1 only)
+ `CHANGELOG.md` — all trivial conflicts per §2. Avoid scheduling two
items from the **same** "Depends on / shares module" note in one wave
(e.g. A5+A6, C1+C2, F3+F4, E3+E4).

---

## 5. Per-PR definition of done

Keep each PR consistent so review stays fast:

- [ ] One tool (or one cohesive helper set) per PR — small and focused.
- [ ] New code in a new module where it forms a cluster; otherwise the
      smallest possible edit to the relevant existing module.
- [ ] Tool registered in the right family registrar in `tools.py`,
      gated correctly (read / WRITE / DDL / SHELL).
- [ ] `available=false` (not an error) when an optional extension is
      absent, matching the existing search/vector tools.
- [ ] Unit tests via `FakeDriver` / `FakeRoutingDriver`; cover the
      unavailable path, the happy path, and argument validation.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy src/mcpg`
      all clean; full unit suite green; new module at ~100% unit coverage.
- [ ] Docs: add the tool line to `docs/tour.md` and flip its
      `feature-shortlist.md` row to ✅. **Leave the tool count alone**
      (see §2). Add a `CHANGELOG.md` `[Unreleased]` bullet.
- [ ] Branch `claude/<short-name>` (or your convention); squash-merge
      with a `(#N)` title.

---

## 6. Tool-count reconciliation

Because feature PRs deliberately skip the tool-count bump (§2), run a
periodic sync PR:

```bash
grep -c '@server.tool' src/mcpg/tools.py
# update the "NNN tools" line in docs/tour.md, docs/user-guide.md, docs/tools.md
```

Do this once per merged batch rather than per feature PR.
