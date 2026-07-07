# MCPg release process — PyPI publishing playbook

A living guide for cutting an MCPg release and shipping it to
[PyPI](https://pypi.org/). Covers the one-time account / project setup,
the recurring pre-flight checks, the recommended automation
(GitHub Actions + PyPI Trusted Publishing via OIDC — no long-lived
API tokens stored as secrets), and the rollback playbook.

The first release uses this guide end-to-end. Subsequent releases
trim down to **§5 Pre-flight checklist** + **§6 Cutting the release**.

---

## Table of contents

1. [Goals + non-goals](#1-goals--non-goals)
2. [One-time account + project setup](#2-one-time-account--project-setup)
3. [`pyproject.toml` metadata audit](#3-pyprojecttoml-metadata-audit)
4. [Local build + TestPyPI dry-run](#4-local-build--testpypi-dry-run)
5. [Pre-flight checklist](#5-pre-flight-checklist)
6. [Cutting the release](#6-cutting-the-release)
7. [Post-publish smoke test](#7-post-publish-smoke-test)
8. [Rollback playbook](#8-rollback-playbook)
9. [Recurring chores](#9-recurring-chores)

---

## 1. Goals + non-goals

### Goals

- `pip install mcpg` works on any supported platform (Linux + macOS +
  Windows, Python 3.12+).
- Every release on PyPI corresponds to a signed Git tag on `main`.
- Releases ship from CI via **PyPI Trusted Publishing** (OIDC) — no
  PyPI API tokens stored as GitHub Action secrets.
- Each release passes the same gates as every PR plus an extra
  TestPyPI install-and-import smoke test before flipping to prod.

### Non-goals (yet)

- Wheels for binary extension modules — MCPg is pure Python; the
  `psycopg[binary]` and `pyjwt[crypto]` extras pull their own wheels.
- Conda-forge packaging. Filed as a follow-up if there's demand.
- Multi-version Python wheels — the `py3-none-any` tag suffices since
  there are no compiled artifacts.

---

## 2. One-time account + project setup

These steps happen exactly once when MCPg first lands on PyPI.

### 2.1 Reserve the project on PyPI + TestPyPI

1. Sign in at <https://pypi.org/> with the maintainer account
   (`devopam@gmail.com`). Enable 2FA — required by PyPI for any
   account that maintains a project since 2024.
2. Do the same on <https://test.pypi.org/> with a separate account
   if you don't already have one. TestPyPI is the staging environment;
   uploading bad metadata there has zero consequences.
3. **Don't** create the project on the website. The first
   `twine upload` (§4.5) registers the name implicitly. Doing it
   that way means the package metadata comes from `pyproject.toml`
   alone, not from a web form that can drift.

### 2.2 Enable PyPI Trusted Publishing (OIDC)

This is the modern way to authorize a GitHub Action to upload a
release — no API token ever leaves PyPI. The action exchanges its
short-lived OIDC token for a one-time PyPI upload credential.

On <https://pypi.org/manage/account/publishing/> (for the
`devopam@gmail.com` account):

1. Click **Add a new pending publisher**.
2. Fill in:
   - **PyPI project name:** `mcpg`
   - **Owner:** `devopam`
   - **Repository name:** `MCPg`
   - **Workflow filename:** `publish.yml`
   - **Environment name:** `pypi` (must match `environment:` in the
     workflow, see §6.1)
3. Repeat on <https://test.pypi.org/manage/account/publishing/> with
   the same settings but **Environment name:** `testpypi`.

Once the first matching workflow run uploads to PyPI, the pending
publisher becomes a confirmed publisher and the project is permanently
linked.

### 2.3 Create GitHub Actions environments

In the `devopam/MCPg` repo:

1. **Settings → Environments → New environment** → name it `pypi`.
2. Add a **Required reviewer** (the maintainer's GitHub account). This
   forces a manual approval click between tag-push and upload — a
   small friction that prevents accidental `v0.1.0-typo` from going
   live.
3. Repeat for `testpypi` but leave it *without* required reviewers —
   staging should be friction-free.

> **Why environments and not just secrets?** Environments add the
> reviewer gate, scope OIDC claims (`environment:pypi` lands inside
> the JWT and PyPI verifies it), and let the production-publish job
> require a specific deployment branch (`main` only).

### 2.4 Configure branch protection on `main`

Already mostly in place — confirm:

- `Lint & type-check`, `Tests (PG 14|15|16|17|18)`, and
  `Security audit (SAST & Dependencies)` are **required status
  checks**.
- **Require linear history** is on (rebase merges only).
- **Require signed commits** is on. PyPI auto-attests releases when
  the publishing workflow runs from a verified-tag commit, so signing
  is on the critical path.
- **Restrict who can push to matching branches**: the maintainer
  account only.

---

## 3. `pyproject.toml` metadata audit

PyPI surfaces the metadata block on the project's
`https://pypi.org/project/mcpg/` page. Anything missing here is
missing from the marketing surface — and from the
`pip show mcpg` output for end users.

### 3.1 Currently in `pyproject.toml`

> **Historical (pre-first-release audit).** Today `pyproject.toml` no
> longer carries a static `version`; it declares `dynamic = ["version"]`
> with `[tool.hatch.version] path = "src/mcpg/__init__.py"`, so the
> version is single-sourced from `__init__.py` (see §4.2). Don't
> reintroduce a static `version` field from the snippet below.

```toml
[project]
name = "mcpg"
version = "0.5.0"
description = "A production-grade PostgreSQL Model Context Protocol (MCP) server."
readme = "README.md"
requires-python = ">=3.12"
license = "MIT"
license-files = ["LICENSE", "src/mcpg/_vendor/LICENSE"]
```

### 3.2 What to add before the first release

The block below is the *minimum* upgrade. Each line maps to a PyPI
page field — comments call out what they affect.

```toml
[project]
name = "mcpg"
version = "0.5.0"
description = "A production-grade PostgreSQL Model Context Protocol (MCP) server."
readme = "README.md"                              # → "Project description" tab
requires-python = ">=3.12"
license = "MIT"
license-files = ["LICENSE", "src/mcpg/_vendor/LICENSE"]

# Surfaces under "Author" on the project page + in `pip show`.
authors = [
    {name = "Devopam Mittra", email = "devopam@gmail.com"},
]
maintainers = [
    {name = "Devopam Mittra", email = "devopam@gmail.com"},
]

# Powers PyPI's search index + the chip-row at the top of the page.
keywords = [
    "postgresql", "postgres", "mcp", "model-context-protocol",
    "ai-agents", "llm-tools", "database", "claude", "anthropic",
    "dba", "sql", "rag",
]

# Powers the "Classifiers" sidebar + PyPI's faceted search.
classifiers = [
    "Development Status :: 4 - Beta",
    "Intended Audience :: Developers",
    "Intended Audience :: System Administrators",
    "License :: OSI Approved :: MIT License",
    "Operating System :: OS Independent",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3 :: Only",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Programming Language :: SQL",
    "Topic :: Database",
    "Topic :: Database :: Database Engines/Servers",
    "Topic :: Software Development :: Libraries :: Python Modules",
    "Topic :: System :: Systems Administration",
    "Typing :: Typed",
]

# Surfaces as the "Project links" sidebar. Order matters — first
# entry is also the "Homepage" link in the page header.
[project.urls]
Homepage = "https://github.com/devopam/MCPg"
Documentation = "https://github.com/devopam/MCPg#documentation"
Repository = "https://github.com/devopam/MCPg"
Issues = "https://github.com/devopam/MCPg/issues"
Changelog = "https://github.com/devopam/MCPg/blob/main/CHANGELOG.md"
"Release notes" = "https://github.com/devopam/MCPg/releases"
Security = "https://github.com/devopam/MCPg/blob/main/SECURITY.md"
```

### 3.3 README needs a hero block for the PyPI page

PyPI renders the README *exactly* as it appears in the source — no
GitHub-specific rewrites. Two things to verify:

1. **Badges resolve.** GitHub-relative image links break on PyPI.
   Use absolute `https://...` URLs for every badge / asset.
2. **Long-description content-type** is auto-detected from
   `readme = "README.md"`. Confirm with
   `uv run python -m build --sdist && tar tzf dist/*.tar.gz | head`
   that `PKG-INFO` lists `Description-Content-Type: text/markdown`.

### 3.4 Drop dev-only files from the wheel

`hatchling` already includes only `src/mcpg/` per
`[tool.hatch.build.targets.wheel] packages = ["src/mcpg"]`. Confirm
the sdist tarball doesn't bundle `.github/`, `tests/`, `docs/`, etc.
The sdist is meant to be reproducible from a checkout — those
directories make it bulkier without helping reinstalls. Add this to
`pyproject.toml` if needed:

```toml
[tool.hatch.build.targets.sdist]
include = [
    "src/mcpg",
    "README.md",
    "LICENSE",
    "NOTICE",
    "CHANGELOG.md",
    "SECURITY.md",
    "pyproject.toml",
]
```

---

## 4. Local build + TestPyPI dry-run

Always rehearse on TestPyPI before touching production PyPI. The
rehearsal is cheap and catches every "oh, the README has a broken
image" / "the description text-content-type is wrong" footgun.

### 4.1 Install build + twine

```bash
uv sync                                        # locked dev deps
uv add --dev build twine                       # one-time
```

Add `build>=1.2` and `twine>=5.1` to `[dependency-groups].dev` in
`pyproject.toml` once they're in.

### 4.2 Bump the version

The version is **single-sourced** in `src/mcpg/__init__.py`
(`__version__ = "X.Y.Z"`). `pyproject.toml` declares it `dynamic` and
hatchling reads it from that file, so **bump `__init__.py` and nothing
else** — the PyPI metadata, `mcpg --version`, and the MCP `serverInfo`
handshake all derive from that one line and can't drift apart.

Then update `CHANGELOG.md`: rename the `[Unreleased]` heading to
`[X.Y.Z] - YYYY-MM-DD`, add a fresh empty `[Unreleased]` above it.

> **SemVer reminder.** Adding a new tool / env var that defaults to
> the previous behaviour is a **MINOR** bump. Renaming an env var,
> tightening a default like the PG-TLS enforcement, dropping a tool,
> or changing a tool's argument schema is **MAJOR**. Pure bugfixes /
> doc updates / dependency floor bumps are **PATCH**.

### 4.3 Build sdist + wheel

```bash
rm -rf dist/
uv run python -m build
ls dist/
# mcpg-0.5.1-py3-none-any.whl
# mcpg-0.5.1.tar.gz
```

### 4.4 Inspect the artifacts

```bash
# Confirm metadata renders the way PyPI will render it.
uv run twine check dist/*

# Eyeball the sdist contents — make sure secrets / .git / .venv
# never sneak in.
tar tzf dist/mcpg-0.5.1.tar.gz | sort | head -40

# Eyeball the wheel — should be exactly the src/mcpg/ tree.
unzip -l dist/mcpg-0.5.1-py3-none-any.whl | head -30
```

### 4.5 Upload to TestPyPI

For the manual path (preferred only for the very first release;
subsequent releases run §6 automation):

```bash
# Use an API token from https://test.pypi.org/manage/account/token/
# scoped to "Entire account" the first time, then narrow to the
# mcpg project for subsequent uploads.
uv run twine upload --repository testpypi dist/*
```

Land the URL: `https://test.pypi.org/project/mcpg/0.5.1/`. Open it in
a browser. Check:

- README renders fully (no broken image / link).
- Classifiers + keywords show up in the sidebar.
- Project URLs (Homepage / Issues / Security) link out correctly.

### 4.6 Install from TestPyPI in a fresh venv

```bash
mkdir -p /tmp/mcpg-smoke && cd /tmp/mcpg-smoke
uv venv .venv
. .venv/bin/activate

# Two-step install closes a **dependency confusion** vector:
# TestPyPI is a public sandbox where ANYONE can register a package,
# so an `--extra-index-url=pypi` combined with `--index-url=testpypi`
# would let an attacker shadow real PyPI deps (e.g. publishing a
# `pglast==99.99.0` on TestPyPI that pip silently prefers). Instead:
#
#   1. Resolve the runtime deps from the locked source-tree manifest
#      against the **real** PyPI only.
#   2. Install MCPg itself from TestPyPI with --no-deps.
#
# Run step 1 from a checkout of the source tree so the dep list
# stays canonically derived from pyproject.toml.
(cd /path/to/MCPg && uv export \
    --no-dev --no-emit-project --no-hashes \
    --format requirements-txt) > /tmp/mcpg-deps.txt
pip install -r /tmp/mcpg-deps.txt

pip install --no-deps \
    --index-url https://test.pypi.org/simple/ \
    mcpg==0.5.1

python -c "import mcpg; print(mcpg.__version__)"
mcpg --help
```

If any of the above produces an unexpected import error or a missing
console-script, **stop**: fix in source, bump to `X.Y.Z.dev1`, push
to TestPyPI again. TestPyPI never expires; treat the dev-suffix as
disposable.

---

## 5. Pre-flight checklist

Run through this list every release. The first eight items are also
the gates the CI workflow enforces — listed here for transparency
when running locally.

- [ ] Working tree clean; `git status` reports no dirty files.
- [ ] On `main`, fully up-to-date with `origin/main`.
- [ ] `uv sync` resolves cleanly (no version conflicts).
- [ ] `uv run ruff check . && uv run ruff format --check .` clean.
- [ ] `uv run mypy src/mcpg` clean.
- [ ] `uv run pytest tests/unit -q` — entire unit suite passes.
- [ ] `uv run pytest tests/integration -q` against the supported PG
      matrix (run via the `ci.yml` integration job — locally only
      if you have a `MCPG_TEST_DATABASE_URL`).
- [ ] `uv run bandit -r src/mcpg --skip B101,B608,B110 -ll` clean.
- [ ] `uv export --no-dev --no-emit-project --format requirements-txt
      > /tmp/r.txt && uv run pip-audit --strict --disable-pip -r
      /tmp/r.txt` reports no known CVEs.
- [ ] `__version__` in `src/mcpg/__init__.py` is bumped to the release
      (this is the single source; `pyproject.toml` derives it dynamically).
- [ ] `CHANGELOG.md` rolls `[Unreleased]` into the new dated section,
      and a fresh `[Unreleased]` is added on top.
- [ ] `docs/release-notes-X.Y.Z.md` written (mirror the previous
      version's file for structure/tone) **and** linked at the top of
      the "Release notes" list in `docs/index.md`.
- [ ] README hero + badges + screenshots — every relative URL has
      been swapped for an absolute one (`https://...`).
- [ ] `uv run python -m build && uv run twine check dist/*` clean.
- [ ] `git tag vX.Y.Z` — annotated, signed, message is the release
      headline.

---

## 6. Cutting the release

This is the recurring path after the one-time setup.

### 6.1 Add the publishing workflow

Land this once at `.github/workflows/publish.yml`. After it's on
`main`, every `vX.Y.Z` tag triggers the pipeline.

> ⚠️ **The YAML below is the original illustrative sketch and has since
> drifted — do NOT copy it verbatim.** The live workflow
> [`.github/workflows/publish.yml`](../.github/workflows/publish.yml) is
> authoritative: it adds a tag↔`mcpg.__version__` sanity-check step, the
> `.mcpb` build, and the `publish-mcp-registry` / `publish-ghcr` /
> `publish-smithery` jobs (8 jobs total, not 5), and uses current action
> pins (checkout@v7, setup-uv@v7, Python 3.14). Read the real file.

```yaml
name: Publish

on:
  push:
    tags: ["v*.*.*"]

# Required for the OIDC token exchange with PyPI.
permissions:
  id-token: write
  contents: read

jobs:
  build:
    name: Build sdist + wheel
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"
          enable-cache: true
      - run: uv sync --frozen
      - name: Build
        run: uv run python -m build
      - name: Inspect
        run: uv run twine check dist/*
      - uses: actions/upload-artifact@v4
        with:
          name: dist
          path: dist/

  publish-testpypi:
    name: Publish → TestPyPI
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: testpypi
      url: https://test.pypi.org/project/mcpg/
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: dist
          path: dist/
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          repository-url: https://test.pypi.org/legacy/

  smoke-testpypi:
    name: Smoke-test TestPyPI install
    needs: publish-testpypi
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"
          enable-cache: true
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install runtime deps from real PyPI (anti-confusion)
        # TestPyPI is a public sandbox; combining its index with PyPI
        # via --extra-index-url lets an attacker shadow our deps with
        # a higher-numbered fake. Pin step 1 to real PyPI only,
        # derived canonically from pyproject.toml via `uv export`.
        run: |
          uv export --no-dev --no-emit-project --no-hashes \
            --format requirements-txt > /tmp/mcpg-deps.txt
          python -m pip install -r /tmp/mcpg-deps.txt
      - name: Install mcpg from TestPyPI (no deps)
        run: |
          # GITHUB_REF_NAME = "v0.5.1" → strip the leading "v".
          VER="${GITHUB_REF_NAME#v}"
          python -m pip install --no-deps \
            --index-url https://test.pypi.org/simple/ \
            "mcpg==${VER}"
      - name: Import + CLI smoke
        run: |
          python -c "import mcpg; print(mcpg.__version__)"
          mcpg --version

  publish-pypi:
    name: Publish → PyPI (requires reviewer approval)
    needs: smoke-testpypi
    runs-on: ubuntu-latest
    environment:
      name: pypi
      url: https://pypi.org/project/mcpg/
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: dist
          path: dist/
      - uses: pypa/gh-action-pypi-publish@release/v1

  github-release:
    name: Create GitHub Release
    needs: publish-pypi
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: dist
          path: dist/
      - name: Cut release
        env:
          GH_TOKEN: {% raw %}${{ secrets.GITHUB_TOKEN }}{% endraw %}
        run: |
          VER="${GITHUB_REF_NAME#v}"
          # Pull the matching section out of CHANGELOG.md as the
          # release body. Falls back to a stub if the section is
          # missing (forces the maintainer to fix the CHANGELOG).
          awk "/^## \[${VER}\]/,/^## \[/" CHANGELOG.md \
            | sed '$d' > /tmp/notes.md
          [ -s /tmp/notes.md ] || echo "See CHANGELOG.md" > /tmp/notes.md
          gh release create "$GITHUB_REF_NAME" dist/* \
            --title "MCPg ${GITHUB_REF_NAME}" \
            --notes-file /tmp/notes.md
```

> **Reviewer gate.** The `publish-pypi` job stalls on a maintainer
> click in **Actions → publish.yml → Review pending deployments**
> for the `pypi` environment. This is the last chance to cancel
> if the TestPyPI smoke test passed but the maintainer notices a
> blocker on the project page. The `testpypi` environment is
> unrestricted so staging stays frictionless.

### 6.2 Tag + push

After §5's pre-flight is green:

```bash
# Annotated, signed tag — the signature is what PyPI's auto-attest
# step verifies via the OIDC trust chain.
git tag -s vX.Y.Z -m "MCPg vX.Y.Z"

# Push the tag explicitly. CI watches for tag pushes, not commit
# pushes, on the publish workflow.
git push origin vX.Y.Z
```

### 6.3 Approve the production deployment

GitHub emails the maintainer and posts a banner on the workflow run.
Click through to **Review pending deployments**, confirm the version
+ environment, click **Approve and deploy**.

The action exchanges its OIDC token for a one-time upload credential,
uploads to PyPI, then triggers the `github-release` job which posts
the binary artifacts + a release notes section pulled straight from
`CHANGELOG.md`.

---

## 7. Post-publish smoke test

Run from a fresh machine / container that has never seen MCPg:

```bash
docker run --rm -it python:3.12-slim bash -c '
  pip install mcpg=='"$VER"' &&
  mcpg --version &&
  python -c "from mcpg.config import load_settings; print(\"import ok\")"
'
```

If it succeeds, post the new-release announcement to:

- The repo's GitHub Release page (the workflow already created it).
- The README's "What's new" line / hero badge.

> The official MCP registry, PyPI, GHCR, and Smithery are published
> **automatically** by the tagged release (see §8b) — no manual PR or
> upload. The old "file a PR against `modelcontextprotocol/servers`"
> step is obsolete; MCPg publishes its own `server.json` via the
> `publish-mcp-registry` job.

---

## 8. Rollback playbook

### 8.1 The release is bad and not yet downloaded

You have **15 minutes** after upload to delete the release file from
PyPI without filling out the support form. The PyPI UI grants a
"Delete release" button under
**Manage project → Releases → vX.Y.Z** during that window.

After 15 minutes, files become immutable. Delete the *release row*
(not just the file) to remove it from `pip install mcpg` resolution,
but the version number is permanently burnt — you can never re-upload
`vX.Y.Z`, even after delete.

### 8.2 The release is bad and already downloaded by users

1. **Yank the release**:
   `pip install` will refuse to resolve it unless explicitly
   requested. `pip install mcpg==X.Y.Z` keeps working for users who
   pin (so don't surprise them silently); `pip install mcpg` skips it.
2. **Cut a new patch** (`X.Y.Z+1`) immediately with the fix. Follow
   the normal §6 path.
3. **Document the yank** in `CHANGELOG.md` under the bad version's
   heading with `**Yanked.** Reason: …` — preserves the history.

### 8.3 The git tag is wrong but the upload hasn't started

Tag pushes trigger the workflow on tag *creation*, not amendment.

```bash
git tag -d vX.Y.Z              # local
git push origin :refs/tags/vX.Y.Z   # remote
# Re-tag the correct commit, re-push.
```

If the workflow already started, cancel it from **Actions → publish.yml
→ running** before approving the `pypi` environment.

---

## 8b. Registry & directory listings

Where MCPg is listed, and what a release does to each — so nothing is
silently stale and the few manual steps aren't forgotten. **Most of
this is automatic**; the publish workflow fans a tagged release out to
every machine-updatable surface.

| Listing | Update mechanism | Per-release action |
|---|---|---|
| **PyPI** | `publish-pypi` job (OIDC, maintainer-gated) | Automatic |
| **GHCR** (`ghcr.io/devopam/mcpg`) | `publish-ghcr` job | Automatic |
| **GitHub Releases** (+ `.mcpb` asset) | `github-release` job | Automatic |
| **Official MCP Registry** | `publish-mcp-registry` job (`server.json`, version-synced) | Automatic |
| **PulseMCP** | Ingests the MCP Registry daily | Automatic (transitive) |
| **mcp.so / mcpservers.org / Glama** | Scrape GitHub + the registry | Automatic (transitive) |
| **Smithery** (`devopam/mcpg`) | `publish-smithery` job (opt-in) | Automatic *once enabled* — see below |
| **Hosted demo** (HF Space `devopam/mcpg-demo`) | `publish-hf-space` job (opt-in) | Automatic *once enabled* — see below |
| **Claude connectors directory** | Review portal, no API | **Manual** — see below |

### Copy that lives in `server.json` (drives the registry-fed listings)

The MCP Registry — and everything that ingests it (PulseMCP, aggregators)
— sources its blurb from `server.json`'s `description` (≤100 chars,
enforced by the registry schema). It is **not** auto-generated, so when
the tool count or positioning changes, edit `server.json` and it
propagates on the next release.

### Enabling the Smithery auto-publish

The `publish-smithery` job is inert until you switch it on:

1. Create a Smithery API key at **smithery.ai** → account → API Keys.
2. Add it as a repo **secret** named `SMITHERY_API_KEY`
   (`Settings → Secrets and variables → Actions → Secrets`).
3. Add a repo **variable** `PUBLISH_SMITHERY` = `true`
   (same page → Variables).

The job derives a Smithery-compatible bundle from `packaging/mcpb` via
`packaging/smithery/build.py` (Smithery doesn't understand the canonical
bundle's `uv` server type, so the variant uses `python` + a direct
`uvx mcpg` launch) and publishes it. Because the listing launches
`uvx mcpg` (always latest), it already tracks PyPI between releases —
the job just keeps the declared version and metadata current.

> **First-time Smithery listing** is done by hand (`smithery mcp publish`);
> the job maintains it thereafter. The listing's **description and icon**
> aren't set by bundle-publish — set those once in Smithery's web
> server-card settings (they persist across re-publishes).

### Refreshing the hosted demo (HF Space)

The public read-only demo at `https://devopam-mcpg-demo.hf.space/mcp`
runs the `ghcr.io/devopam/mcpg:latest` image. Two things mean it does
**not** pick up a new release on its own:

1. `:latest` is only rebuilt by the `publish-ghcr` job — i.e. on the
   release tag — so between releases it stays on the last shipped image.
2. Hugging Face Docker Spaces resolve `FROM …:latest` at **build** time
   and cache the layer; a new GHCR push does not auto-repull.

The **`publish-hf-space` job** handles this automatically after
`publish-ghcr`, once enabled. To enable it: add an **`HF_TOKEN`** repo
secret (a write token scoped to the `devopam/mcpg-demo` Space) and set
the **`REFRESH_HF_SPACE`** repo variable to `true`. The job is
`continue-on-error`, so a demo hiccup never fails a release.

If it's not enabled (or you need an ad-hoc rebuild), trigger a **Factory
rebuild** manually — the Space's **Settings → Factory rebuild**, or via
the HF API:

```python
from huggingface_hub import HfApi
HfApi(token="hf_…").restart_space("devopam/mcpg-demo", factory_reboot=True)
```

Only then does the live demo (and its `serverInfo` version) match the
release. Nothing else about the demo changes between releases.

### The reviewed submission: Claude connectors directory

The Claude directory is a **reviewed** submission portal with no
publish API, so releases can't push to it automatically. After a
release that changes the tool surface or the `.mcpb` in a way worth
re-listing:

1. Grab the new `mcpg-<version>.mcpb` from the GitHub release.
2. Re-submit via the desktop-extension form linked from the
   [connectors submission docs](https://claude.com/docs/connectors/building/submission).
3. Track status in Claude's submissions dashboard; escalate to
   `mcp-review@anthropic.com` if stuck.

Because the bundle installs `mcpg` from PyPI (always the pinned release
at install time), a listed extension keeps working across patch
releases without re-submission — re-submit only for material changes.

---

## 9. Recurring chores

Quarterly housekeeping items that don't block a release but keep the
publishing surface healthy:

- **Refresh classifiers** when a new Python version reaches "3.x" RC
  (add `Programming Language :: Python :: 3.x`).
- **Upgrade `pypa/gh-action-pypi-publish`** to the latest `release/v1`
  channel — they ship Trusted-Publishing security improvements
  out-of-band of MCPg.
- **Rotate the maintainer's PyPI 2FA recovery codes** annually.
- **Audit project collaborators** on `pypi.org/manage/project/mcpg/`
  — make sure the only owner is the maintainer's account.
- **Re-run `pip-audit`** on the last shipped requirements set; if a
  CVE has landed since release, cut a patch.
- **Sweep the NL→SQL provider default models (~quarterly).** Vendors
  retire models on their own cadence (e.g. Cerebras dropped
  `llama-3.1-8b`), which would leave a built-in provider pointing at a
  dead default. Check each entry's `default_model` in
  `src/mcpg/nl2sql.py`'s `_PROVIDERS` registry against the vendor's live
  models page and bump any that changed. It's a pure data edit — one
  string per affected provider, no code — then cut a build. Base URLs
  and key env vars are stable and rarely need attention.
- **Rotate the `SMITHERY_API_KEY`** secret if it's ever been exposed
  (e.g. pasted into a chat/issue); regenerate at smithery.ai and update
  the repo secret.

---

## See also

- [`SECURITY.md`](../SECURITY.md) — vulnerability reporting policy.
- [`docs/security-hardening.md`](security-hardening.md) — security
  hardening roadmap. Several queued items have user-visible knobs
  that will need release notes.
- [`CHANGELOG.md`](../CHANGELOG.md) — the source of truth for what
  a given version shipped.
- [`docs/installation.md`](installation.md) — end-user install guide;
  add a "Install from PyPI" section after the first publish.
- [PyPI Trusted Publishing docs](https://docs.pypi.org/trusted-publishers/)
- [PEP 621 — Storing project metadata in `pyproject.toml`](https://peps.python.org/pep-0621/)
