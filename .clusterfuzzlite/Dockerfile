# ClusterFuzzLite build integration for the SQL-safety kernel
# (mcpg.sql.safety). See docs/reviews/devendor-sql-kernel-security-review.md
# for why this parser+AST-walker is the project's security-critical
# surface. Atheris and the OSS-Fuzz Python toolchain are pre-installed on
# this base image — see
# https://google.github.io/oss-fuzz/getting-started/new-project-guide/python-lang/
FROM gcr.io/oss-fuzz-base/base-builder-python

COPY . $SRC/mcpg
WORKDIR $SRC/mcpg
COPY .clusterfuzzlite/build.sh $SRC/
COPY .clusterfuzzlite/sql_safety_fuzzer.py $SRC/
