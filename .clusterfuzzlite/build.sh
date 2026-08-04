#!/bin/bash -eu
# Standard OSS-Fuzz Python build script — see
# https://google.github.io/oss-fuzz/getting-started/new-project-guide/python-lang/
# for what each step does and why.

# Build and install mcpg (using current CFLAGS/CXXFLAGS) so its C-extension
# deps (pglast wraps libpg_query; psycopg's C extension) are compiled with
# the sanitizer this run is instrumented with.
pip3 install .

for fuzzer in $(find "$SRC" -name '*_fuzzer.py'); do
  fuzzer_basename=$(basename -s .py "$fuzzer")
  fuzzer_package=${fuzzer_basename}.pkg

  # Standalone package via pyinstaller, to avoid Python-version/environment
  # drift between build time and whenever ClusterFuzzLite replays this
  # binary later.
  pyinstaller --distpath "$OUT" --onefile --name "$fuzzer_package" "$fuzzer"

  # Execution wrapper: Atheris needs the sanitizer runtime preloaded, and
  # this is the file ClusterFuzzLite actually invokes as the fuzz target.
  echo "#!/bin/sh
this_dir=\$(dirname \"\$0\")
LD_PRELOAD=\$this_dir/sanitizer_with_fuzzer.so \
ASAN_OPTIONS=\$ASAN_OPTIONS:symbolize=1:external_symbolizer_path=\$this_dir/llvm-symbolizer:detect_leaks=0 \
\$this_dir/$fuzzer_package \$@" > "$OUT/$fuzzer_basename"
  chmod +x "$OUT/$fuzzer_basename"
done
