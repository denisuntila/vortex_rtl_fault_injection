#!/usr/bin/env bash
#
# bisect_loop.sh — repeat bisection rounds (refine_time_sweep.py + run_time_sweep.sh)
# until every window edge is narrower than the resolution, or MAX_ROUNDS is reached.
#
# Usage (from the repo root, inside the container):
#   ./scripts/bisect_loop.sh <db> <bits.tsv> <out_prefix> [refine options...]
#
# Example:
#   ./scripts/bisect_loop.sh tests/ocl_test/sweep_phase1_v2.db sweep_bits_v2.tsv tests/ocl_test/sweep_p2 \
#       --only 'mscratch|warp_pcs|thread_masks|tag_store' \
#       --manifest tests/ocl_test/sweep_p1v2/manifest.tsv --unmatched-masked --resolution 200
#
# Rounds go to <out_prefix>_r1, _r2, ... and all runs into the SAME <db>.
# Environment: SHARDS (default 4), MAX_ROUNDS (default 8), SCHEMA (default tests/ocl_test/schema.sql).
# Interrupted? Relaunch the same command: finished rounds are skipped by the
# resume logic of run_time_sweep.sh and the next round is recomputed from the DB.

set -uo pipefail

DB="${1:-}"; BITS="${2:-}"; PREFIX="${3:-}"
[ -n "$DB" ] && [ -n "$BITS" ] && [ -n "$PREFIX" ] || {
  echo "Usage: $0 <db> <bits.tsv> <out_prefix> [refine options...]" >&2; exit 1; }
shift 3

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARDS="${SHARDS:-4}"
MAX_ROUNDS="${MAX_ROUNDS:-8}"
SCHEMA="${SCHEMA:-tests/ocl_test/schema.sql}"

for ((r = 1; r <= MAX_ROUNDS; r++)); do
  OUT="${PREFIX}_r${r}"
  echo
  echo "################ round $r -> $OUT ################"
  rm -rf "$OUT"
  python3 "$SCRIPT_DIR/refine_time_sweep.py" --db "$DB" --bits "$BITS" \
      -o "$OUT" --shards "$SHARDS" "$@" || exit 1
  if [ ! -f "$OUT/manifest.tsv" ]; then
    echo "Nothing left to refine after $((r - 1)) round(s)."
    break
  fi
  "$SCRIPT_DIR/run_time_sweep.sh" "$OUT" --db "$DB" --schema "$SCHEMA" > "$OUT/run.log" 2>&1
  rc=$?
  grep -E "WARNING|stored .* of" "$OUT/run.log" && { echo "A shard stopped early (see $OUT/run.log)"; exit 1; }
  [ $rc -eq 0 ] || { echo "run_time_sweep.sh failed (see $OUT/run.log)"; exit 1; }
done

echo
echo "################ final windows ################"
python3 "$SCRIPT_DIR/refine_time_sweep.py" --db "$DB" --bits "$BITS" --report-only "$@"