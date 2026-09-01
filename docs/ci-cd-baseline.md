# CI/CD Baseline

**Project:** MCPg
**Baseline created:** 2026-09-01
**Last audited:** 2026-09-01
**ci-cd-plumber skill version:** 0.1.0 (from agent-skills repo at audit time)
**Maturity target:** high-assurance (production, long-lived, multi-channel release)

## Platform & triggers

- **Primary platform:** GitHub Actions
- **CI triggers:** `push` to `main` and `claude/**`; `pull_request` (all)
- **Release trigger:** `push` of tags matching `v*.*.*`
- **Scheduled:** Scorecard (weekly Thu), CodeQL (weekly Wed), Actions-security / zizmor (weekly Mon)
- **Concurrency:** `ci-${{ github.ref }}` with `cancel-in-progress: true` on the main CI workflow

## Pipeline layers (observed)

1. **PR / main validation** (`ci.yml`)
   - `lint` — ruff check + format --check, mypy on `src/mcpg`
   - `security` — pip-audit --strict (runtime deps), bandit SAST, non-blocking pip-licenses
   - `test` — matrix PG 14–18 (required) + experimental PG 19 and WarehousePG; coverage gate fail_under=90
2. **Static / supply-chain security**
   - `codeql.yml` — CodeQL python + actions languages on PR + main + schedule
   - `actions-security.yml` — zizmor (SARIF) + actionlint (reporting mode)
   - `scorecard.yml` — OpenSSF Scorecard, publish_results + code-scanning upload
   - ClusterFuzzLite (`cflite_pr.yml`, `cflite_batch.yml`)
3. **Release / promotion** (`publish.yml` on `v*.*.*` tags)
   - build sdist+wheel → version-tag sanity check → CycloneDX SBOM → build-provenance attestation
   - TestPyPI (OIDC) → smoke install (hash-pinned, simple-index poll) → human-gated PyPI (OIDC environment protection)
   - GitHub Release (notes from CHANGELOG.md section) + attach dist + SBOM + `.mcpb`
   - MCP Registry (OIDC), GHCR image (`:version` + `:latest`), optional Smithery / HF Space refresh
4. **Docs site** — `pages.yml`
5. **Dependency automation** — Dependabot (pip + github-actions + docker), weekly, grouped minor/patch; Conventional Commit prefixes

## Security posture (recorded decisions)

- Workflow default `permissions: contents: read` (or `read-all` where appropriate); elevated scopes only on jobs that need them (`id-token`, `attestations`, `security-events`, `packages`, `contents: write` for release)
- Third-party actions pinned to full commit SHAs with version comments (including `github/codeql-action` init/analyze/upload-sarif)
- `persist-credentials: false` on checkouts
- step-security/harden-runner on every job (egress-policy: **audit** — progressive rollout toward block)
- OIDC Trusted Publishing for PyPI / TestPyPI; OIDC for MCP Registry login and attestations
- CycloneDX SBOM + `actions/attest-build-provenance` on distributions
- CODEOWNERS routes `/.github/workflows/`, SQL kernel, policy, packaging, SECURITY.md to @devopam

## Release documentation

- Keep a Changelog `CHANGELOG.md` (SemVer)
- Tag-driven releases; GitHub Release body extracted from matching CHANGELOG section
- Conventional Commits used by Dependabot and project practice
- Detailed operator playbook: `docs/release-process.md`
- Per-version release notes under `docs/release-notes-*.md` for major milestones
- No release-please / semantic-release automation (manual tag + process by design)

## Deliberate exceptions (do not treat as regressions unless intent changes)

1. **zizmor / actionlint in reporting mode** — see TODO below before promoting to blocking.
2. **Harden-Runner egress-policy: audit** — see TODO below before block mode on publish.
3. **PG 19 and WarehousePG matrix lanes** — `continue-on-error: true` / experimental; non-gating by design until GA / image stability.
4. **Parameterized CI Postgres Dockerfile** (`.github/ci-postgres.Dockerfile`) — intentionally not digest-pinned (driven by `PG_MAJOR` matrix); other Dockerfiles use digests where fixed.
5. **Docker build-push `provenance: false`** on GHCR job — intentional to keep package page as a single clean manifest.
6. **Manual tag-based release** rather than release-please — matches documented release-process.md; not a gap unless automation is later desired.

## TODO (deferred from 2026-09-01 ci-cd-plumber audit)

Do **not** treat these as open defects until you choose to schedule them. Order matches increasing operational risk.

1. **[M2] Promote zizmor / actionlint to blocking**
   - Precondition: confirm Security → Code scanning (zizmor category) is clean, or triage remaining findings / waivers.
   - Then in `.github/workflows/actions-security.yml`: remove `continue-on-error: true` on the zizmor step; set actionlint `fail-on-error: true`.
   - Avoid promoting while known open findings remain (otherwise every PR fails for no new value).

2. **[L1] Harden-Runner block mode on `publish.yml` first**
   - Precondition: review egress audit logs from real publish runs; build an allow-list covering PyPI, TestPyPI, GHCR, MCP registry, uv/npx, attestation endpoints, etc.
   - Only then switch `egress-policy: block` on publish jobs (not on guesswork — a bad list breaks tagged releases).

3. **Optional (leave unless priorities change)**
   - **[L2]** Path filters / `paths-ignore` on `ci.yml` for pure-docs changes (trade-off: some doc PRs skip the test matrix).
   - **[L3]** Re-enable Docker build-push provenance on GHCR if multi-manifest package UI is acceptable.
   - **[L4]** release-please / similar only if release cadence or contributor count grows enough to justify automation.

## Drift log

- 2026-09-01: Baseline created retroactively by ci-cd-plumber. First full audit run in the same invocation. Findings reported in conversation; no bulk auto-fixes applied.
- 2026-09-01: **M1 remediated** — `github/codeql-action/init` and `analyze` in `.github/workflows/codeql.yml` pinned from mutable `@v4` tags to full SHA `cdf488f595d80d6e07e03d4674febd5ab45fa938` (# v4), matching existing `upload-sarif` pins elsewhere.
- 2026-09-01: Remaining audit items (M2, L1, optional L2–L4) recorded under **TODO (deferred…)**; no further changes in this pass.
