#!/usr/bin/env bash
#
# run_sdc_recheck_parallel.sh — launch m instances of run_sdc_recheck.sh at once.
#
# Instance n (0..m-1) handles the files whose number % m == n, exactly as if
# you had started `run_sdc_recheck.sh <pairs_dir> n m` by hand in m shells.
# Every instance logs to its own file; the launcher waits for all of them and
# prints an aggregate summary at the end. Ctrl-C stops every instance.
#
# Usage (from the repo root):
#   ./scripts/run_sdc_recheck_parallel.sh <pairs_dir> [m]     (default m = nproc)
#
# Examples:
#   ./scripts/run_sdc_recheck_parallel.sh sdc_pairs 4
#   nohup ./scripts/run_sdc_recheck_parallel.sh sdc_pairs 4 > recheck_launcher.log 2>&1 &
#
# Environment overrides:
#   DB        Shared SQLite DB for all instances
#             (default: tests/ocl_test/recheck_campaign.db)
#   LOG_DIR   Where per-instance logs go (default: tests/ocl_test/recheck_logs)
#
# Resume: relaunch with the SAME m. Each instance skips the files already in
# its results TSV (tests/ocl_test/recheck_results_<n>of<m>.tsv). Changing m
# changes both the partition and the TSV names, so previous progress is not
# picked up.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="${WRAPPER:-$SCRIPT_DIR/run_sdc_recheck.sh}"

usage() {
  echo "Usage: $0 <pairs_dir> [m]   (default m = nproc)" >&2
  exit 1
}

PAIRS_DIR="${1:-}"
M="${2:-$(nproc)}"

[ -z "$PAIRS_DIR" ] && usage
[ -d "$PAIRS_DIR" ] || { echo "Not a directory: $PAIRS_DIR" >&2; exit 1; }
[[ $M =~ ^[1-9][0-9]*$ ]] || { echo "m must be a positive integer (got '$M')" >&2; exit 1; }
[ -x "$WRAPPER" ] || { echo "Not executable: $WRAPPER" >&2; exit 1; }

export DB="${DB:-tests/ocl_test/recheck_campaign.db}"
unset RESULTS   # each instance must keep its own results TSV
LOG_DIR="${LOG_DIR:-tests/ocl_test/recheck_logs}"
mkdir -p "$LOG_DIR"

# Ctrl-C / SIGTERM: stop the whole process group (all instances and their runs)
trap 'trap - INT TERM; echo; echo "Stopping all instances..."; kill 0' INT TERM

echo "Launching $M instances | pairs_dir=$PAIRS_DIR | DB=$DB | logs=$LOG_DIR"

pids=()
for ((n = 0; n < M; n++)); do
  log="$LOG_DIR/recheck_${n}of${M}.log"
  "$WRAPPER" "$PAIRS_DIR" "$n" "$M" >> "$log" 2>&1 &
  pids+=($!)
  echo "  instance $n/$M  pid ${pids[$n]}  -> $log"
done
echo "Follow one with: tail -f $LOG_DIR/recheck_0of${M}.log"

fail=0
for n in "${!pids[@]}"; do
  wait "${pids[$n]}" || { echo "instance $n/$M exited with status $?"; fail=1; }
done

echo
echo "All instances finished."

tsvs=()
for ((n = 0; n < M; n++)); do
  t="tests/ocl_test/recheck_results_${n}of${M}.tsv"
  [ -f "$t" ] && tsvs+=("$t")
done

if [ ${#tsvs[@]} -gt 0 ]; then
  awk -F'\t' '
    $1 != "file" { files++; if ($2 == "sdc") sdc++; else nosdc++; err += $7 }
    END { printf "Summary: files done %d | SDC reproduced %d | no SDC %d | pair errors %d\n",
                 files + 0, sdc + 0, nosdc + 0, err + 0 }
  ' "${tsvs[@]}"
else
  echo "No results TSV found."
fi

exit $fail

