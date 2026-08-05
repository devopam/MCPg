# ClusterFuzzLite build integration for the SQL-safety kernel
# (mcpg.sql.safety). See docs/reviews/devendor-sql-kernel-security-review.md
# for why this parser+AST-walker is the project's security-critical
# surface. Atheris and the OSS-Fuzz Python toolchain are pre-installed on
# this base image — see
# https://google.github.io/oss-fuzz/getting-started/new-project-guide/python-lang/
#
# Pinned to the digest current as of 2026-08-05 (verified against the gcr.io
# registry, matches Scorecard's own remediation tip). Bump manually when
# OSS-Fuzz ships toolchain/atheris updates — same convention the rest of
# this repo's Dockerfiles already follow.
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:6b2b3a7e4a2da50de47f94925cffbe34747e1aae4f33d7f07ba6c681dd648b23

COPY . $SRC/mcpg
WORKDIR $SRC/mcpg
COPY .clusterfuzzlite/build.sh $SRC/
COPY .clusterfuzzlite/sql_safety_fuzzer.py $SRC/
