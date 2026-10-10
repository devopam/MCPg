# OSS Scanner enrollment

Artifacts for enrolling MCPg in [Anthropic's OSS
Scanner](https://github.com/anthropics/oss-scanner), a service that builds a
project in an isolated, no-network VM and runs a Claude-powered security scan,
emailing findings (with reproduction steps and proposed patches) privately to
the `primary_contact`.

Files here:

- **`Dockerfile`** — the build image the scanner uses. MCPg is pure Python, so
  it just installs the full source + dependency environment (runtime + dev,
  for the adversarial test suites) at build time; the checkout lands at `/src`
  per the scanner contract.
- **`threat_model.md`** — steers the scan at MCPg's real surface: the SQL-safety
  allowlist, access-mode / tenancy gates, OIDC auth, and the shell tools, with
  app-security severity tiers.
- **`project.yaml`** — the enrollment manifest (reference copy; see below).

## How to enroll (maintainer, one-time)

The scanner is enrolled by a pull request **to the `anthropics/oss-scanner`
repo** — which is separate from this one, so it must be opened by a maintainer
(fork + PR):

1. Fork `anthropics/oss-scanner`.
2. Add `projects/mcpg/project.yaml` with the contents of
   [`project.yaml`](project.yaml) in this directory (it already points
   `dockerfile` / `threat_model` at the paths here in `devopam/MCPg`, so the
   scanner pulls them from this repo — nothing else to copy).
3. Optionally run their local checks first: `tools/validate.py` then
   `tools/check mcpg` (needs Docker + Python + PyYAML).
4. Open the PR. After Anthropic merges it, scans run and findings are emailed
   to `primary_contact`.

## Notes

- Enrollment is reviewed/merged by Anthropic and targets "critical" OSS —
  acceptance is their call.
- Reports are model-generated and not human-reviewed (no public disclosure);
  treat findings as leads to verify, per the project's adversarial-test
  discipline.
- To pin the scanned revision, add `#<tag>` to `repo` in `project.yaml`.
