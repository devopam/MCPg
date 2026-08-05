#!/bin/bash -eu
# Standard OSS-Fuzz Python build script — see
# https://google.github.io/oss-fuzz/getting-started/new-project-guide/python-lang/
# for what each step does and why.

# Build and install mcpg (using current CFLAGS/CXXFLAGS) so its C-extension
# deps (pglast wraps libpg_query; psycopg's C extension) are compiled with
# the sanitizer this run is instrumented with.
#
# --ignore-requires-python: base-builder-python ships Python 3.11, below
# this project's declared floor (requires-python >=3.12 in pyproject.toml,
# set for the wider ~254-tool codebase). The fuzz harness only imports
# mcpg.sql.safety + its direct deps (mcpg.sql.allowlist, mcpg.sql.driver),
# none of which use any 3.12-only syntax — verified before adding this
# flag, not assumed. Loosening pyproject.toml's actual floor for the
# whole package would be the wrong fix; this scopes the exception to the
# fuzzing container only.
pip3 install --ignore-requires-python .

# -maxdepth 1: the Dockerfile COPYs the harness to $SRC/ directly, but
# `COPY . $SRC/mcpg` also copies the whole repo (including
# .clusterfuzzlite/sql_safety_fuzzer.py) underneath it — an unbounded
# find would match both copies of the same file and build the fuzzer
# twice, silently overwriting $OUT/sql_safety_fuzzer the second time
# (both matches share the same basename). Confirmed in a real CI run
# before this fix landed (two full pyinstaller passes in one build log).
for fuzzer in $(find "$SRC" -maxdepth 1 -name '*_fuzzer.py'); do
  fuzzer_basename=$(basename -s .py "$fuzzer")
  fuzzer_package=${fuzzer_basename}.pkg

  # Standalone package via pyinstaller, to avoid Python-version/environment
  # drift between build time and whenever ClusterFuzzLite replays this
  # binary later.
  pyinstaller --distpath "$OUT" --onefile --name "$fuzzer_package" "$fuzzer"

  # Execution wrapper: Atheris needs the sanitizer runtime preloaded, and
  # this is the file ClusterFuzzLite actually invokes as the fuzz target.
  # The "LLVMFuzzerTestOneInput" comment isn't decorative — OSS-Fuzz's
  # fuzzer-detection step greps wrapper scripts for that literal string
  # to recognize them as fuzz targets; omitting it fails the build with
  # "No fuzz targets found" even though the binary built fine.
  echo "#!/bin/sh
# LLVMFuzzerTestOneInput for fuzzer detection.
this_dir=\$(dirname \"\$0\")
LD_PRELOAD=\$this_dir/sanitizer_with_fuzzer.so \
ASAN_OPTIONS=\$ASAN_OPTIONS:symbolize=1:external_symbolizer_path=\$this_dir/llvm-symbolizer:detect_leaks=0 \
\$this_dir/$fuzzer_package \$@" > "$OUT/$fuzzer_basename"
  chmod +x "$OUT/$fuzzer_basename"
done
