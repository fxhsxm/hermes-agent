#!/usr/bin/env bash
# One command to re-run the whole memory regression suite and keep its artifacts.
#
#   1. unit invariants  -- the plugin contracts that catch silent memory loss
#   2. health check     -- per-layer live health (write probe, staleness, ops, coverage, stores)
#   3. retrieval quality-- recall@k, duplication, staleness, reranker engagement (weekly)
#
# Usage:
#   bash scripts/run_memory_regression.sh            # full run (from the repo)
#   bash $HERMES_HOME/scripts/memory-ops/run_memory_regression.sh   # operational copy
#   MEMORY_REGRESSION_OUT=~/reg bash scripts/run_memory_regression.sh
#   SKIP_TESTS=1 bash scripts/run_memory_regression.sh
#
# Exit: 0 when every stage passed, 1 when any stage reported a problem. The JSON artifacts in
# $OUT are the durable record: diff today's files against last week's to see drift.
#
# LAYOUT: the tools live beside this script in the operational copy
# ($HERMES_HOME/scripts/memory-ops/) and in scripts/ of the repo. Resolve both instead of
# assuming one, and skip (not fail) the unit tests where no test runner exists - a path bug here
# previously made every stage report a failure that was really a missing file.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
if [ -f "$HERE/memory_health_check.py" ]; then
  TOOLS="$HERE"
else
  TOOLS="$REPO/scripts"
fi
TEST_RUNNER=""
for cand in "$REPO/scripts/run_tests.sh" "$TOOLS/run_tests.sh"; do
  if [ -f "$cand" ]; then TEST_RUNNER="$cand"; break; fi
done
OUT="${MEMORY_REGRESSION_OUT:-$REPO/memory-regression-out}"
BANK="${HS_BANK:-fwh-main}"
API="${HS_API:-http://127.0.0.1:8888}"
# The LIVE question set is memory-derived, so it is not in this repository (which ships
# retrieval_questions.template.json instead). Look for it where the tools run it from, and never
# fall back to the template: measuring with synthetic questions produces numbers that look real.
QUESTIONS="${HS_QUESTIONS:-$HERE/retrieval_questions.json}"
if [ ! -f "$QUESTIONS" ]; then
  for cand in "$TOOLS/retrieval_questions.json" \
              "$REPO/../v2/retrieval_questions.local.json" \
              "$REPO/docs/audits/memory-system-20260912/v2/retrieval_questions.local.json"; do
    if [ -f "$cand" ]; then QUESTIONS="$cand"; break; fi
  done
fi
if [ ! -f "$QUESTIONS" ] || grep -q '"synthetic"[[:space:]]*:[[:space:]]*true' "$QUESTIONS" 2>/dev/null; then
  echo "SKIP: no live question set found (looked at \$HS_QUESTIONS, \$HERE and the audit workspace);"
  echo "      only the synthetic template is present, so the retention stage cannot be run."
  QUESTIONS=""
fi
PY="${PYTHON:-python}"
mkdir -p "$OUT"
STAMP="$(date +%Y%m%d-%H%M%S)"
rc=0

# Native tools (python.exe) cannot resolve MSYS paths like /c/Users/..., so hand them Windows
# paths. cygpath exists on git-bash; anywhere else the paths are already native.
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
OUT_NATIVE="$(native "$OUT")"
QUESTIONS_NATIVE="$(native "$QUESTIONS")"

echo "=== memory regression $STAMP (bank=$BANK api=$API tools=$TOOLS) ==="

if [ "${SKIP_TESTS:-0}" != "1" ]; then
  echo "--- 1/3 unit invariants ---"
  if [ -z "$TEST_RUNNER" ]; then
    echo "unit invariants: SKIPPED (no run_tests.sh found; run this from a repo checkout)"
  elif bash "$TEST_RUNNER" tests/plugins/memory/test_hindsight_provider.py \
        tests/scripts/test_memory_health_check.py tests/scripts/test_compare_retrieval_runs.py \
        tests/scripts/test_memory_backfill.py tests/scripts/test_validate_golden_set.py; then
    echo "unit invariants: PASS"
  else
    echo "unit invariants: FAIL"; rc=1
  fi
fi

echo "--- 2/3 health check ---"
if (cd "$TOOLS" && "$PY" ./memory_health_check.py --bank "$BANK" --api-url "$API" \
      --json "$OUT_NATIVE/health-$STAMP.json"); then
  echo "health check: PASS"
else
  hr=$?
  # 1 = warnings, 2 = degraded: report, keep going, and fail the run.
  echo "health check: exit $hr (1=warnings, 2=degraded)"; rc=1
fi

echo "--- 3/3 retrieval quality ---"
if [ -f "$QUESTIONS" ]; then
  if (cd "$TOOLS" && "$PY" ./measure_retrieval_quality.py --bank "$BANK" --api "$API" \
        --questions "$QUESTIONS_NATIVE" --out "$OUT_NATIVE/retrieval-$STAMP.json"); then
    echo "retrieval quality: PASS"
  else
    echo "retrieval quality: FAIL"; rc=1
  fi
else
  echo "retrieval quality: SKIPPED (no question set at $QUESTIONS)"
  echo "  expected format: {\"questions\": [{\"id\":\"q01\",\"lang\":\"zh-Hant\",\"query\":\"...\",\"expect\":[\"...\"]}]}"
  echo "  (a bare top-level list is also accepted; the harness reports recall@k only for questions that carry expect)"
fi

echo "=== exit $rc — artifacts in $OUT ==="
ls -1 "$OUT" | sed 's/^/  /'
exit $rc
