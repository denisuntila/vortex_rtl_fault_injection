#!/usr/bin/env bash
#
# run_sdc_recheck.sh — wrapper around run_campaign.sh (deterministic mode).
#
# Takes a directory of pairs-files (0001.txt, 0002.txt, ... as produced by
# extract_sdc_pairs.py, one file per SDC found in the multi-injection DB) and,
# for each file, replays its (timestamp, bit_index) pairs one by one as single
# faults. As soon as a pair produces an SDC, the rest of that file is skipped
# and the wrapper moves on to the next file.
#
# Parallelism: instance <n> of <m> only handles files whose number satisfies
#   file_number % m == n
# e.g. with m=4, launch n=0,1,2,3 in four shells; together they cover every
# file exactly once, with no overlap.
#
# Usage (from the repo root, like run_campaign.sh):
#   ./scripts/run_sdc_recheck.sh <pairs_dir> [n] [m]      (defaults: n=0 m=1)
#
# Examples:
#   ./scripts/run_sdc_recheck.sh tests/ocl_test/sdc_pairs
#   ./scripts/run_sdc_recheck.sh tests/ocl_test/sdc_pairs 0 4
#   ./scripts/run_sdc_recheck.sh tests/ocl_test/sdc_pairs 3 4
#
# Environment overrides:
#   DB       SQLite DB for the recheck runs. Default is one DB PER INSTANCE
#            (tests/ocl_test/recheck_campaign_<n>of<m>.db) so parallel
#            instances never contend for the same SQLite file.
#   RESULTS  TSV summary, one row per completed file. Default:
#            tests/ocl_test/recheck_results_<n>of<m>.tsv
#            Files already listed in it are skipped, so an interrupted
#            instance can simply be relaunched with the same arguments.
#
# Only an outcome of "sdc" stops a file; "masked_error" and "crash" keep going.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_CAMPAIGN="${RUN_CAMPAIGN:-$SCRIPT_DIR/run_campaign.sh}"

usage() {
  echo "Usage: $0 <pairs_dir> [n] [m]   (instance n of m; defaults n=0 m=1)" >&2
  exit 1
}

PAIRS_DIR="${1:-}"
N="${2:-0}"
M="${3:-1}"

[ -z "$PAIRS_DIR" ] && usage
[ -d "$PAIRS_DIR" ] || { echo "Not a directory: $PAIRS_DIR" >&2; exit 1; }
[[ $N =~ ^[0-9]+$ && $M =~ ^[0-9]+$ ]] || { echo "n and m must be non-negative integers" >&2; exit 1; }
(( M >= 1 && N < M )) || { echo "Need m >= 1 and 0 <= n < m (got n=$N m=$M)" >&2; exit 1; }
[ -x "$RUN_CAMPAIGN" ] || { echo "Not executable: $RUN_CAMPAIGN" >&2; exit 1; }

export DB="${DB:-tests/ocl_test/recheck_campaign_${N}of${M}.db}"
RESULTS="${RESULTS:-tests/ocl_test/recheck_results_${N}of${M}.tsv}"
mkdir -p "$(dirname "$RESULTS")"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
trap 'echo; echo "Interrupted."; exit 130' INT TERM

echo "Instance $N of $M | pairs_dir=$PAIRS_DIR | DB=$DB | results=$RESULTS"

shopt -s nullglob
files=("$PAIRS_DIR"/*.txt)

n_files=0; n_sdc=0; n_nosdc=0; n_skipped=0

for f in "${files[@]}"; do
  base="${f##*/}"
  [[ $base =~ ^([0-9]+)\.txt$ ]] || continue
  num=$((10#${BASH_REMATCH[1]}))
  (( num % M == N )) || continue

  # Resume support: skip files already recorded in the results TSV
  if [ -f "$RESULTS" ] && awk -F'\t' -v b="$base" '$1==b{found=1} END{exit !found}' "$RESULTS"; then
    n_skipped=$((n_skipped + 1))
    continue
  fi

  mapfile -t pairs < <(grep -v '^[[:space:]]*$' "$f")
  total=${#pairs[@]}
  echo "##### $base ($total pairs)"

  status="no_sdc"; trigger="-"; trig_run="-"; tried=0; errors=0

  for line in "${pairs[@]}"; do
    tried=$((tried + 1))
    printf '%s\n' "$line" > "$TMP"

    out="$("$RUN_CAMPAIGN" deterministic "$TMP" 2>&1 < /dev/null)"
    res_line="$(grep -oE 'Stored run [0-9]+: [A-Za-z_]+' <<< "$out" | tail -n 1)"

    if [ -z "$res_line" ]; then
      errors=$((errors + 1))
      echo "  [$tried/$total] $line -> ERROR (no 'Stored run' line). Last output lines:"
      tail -n 15 <<< "$out" | sed 's/^/    | /'
      continue
    fi

    run_id="$(sed -E 's/^Stored run ([0-9]+): .*/\1/' <<< "$res_line")"
    outcome="${res_line##*: }"
    echo "  [$tried/$total] $line -> $outcome (run $run_id)"

    if [ "$outcome" = "sdc" ]; then
      read -r ts bit _ <<< "$line"
      status="sdc"; trigger="$ts $bit"; trig_run="$run_id"
      break
    fi
  done

  [ -s "$RESULTS" ] || printf 'file\tstatus\ttrigger_pair\trecheck_run_id\ttried\ttotal\terrors\n' > "$RESULTS"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$base" "$status" "$trigger" "$trig_run" "$tried" "$total" "$errors" >> "$RESULTS"

  n_files=$((n_files + 1))
  if [ "$status" = "sdc" ]; then
    n_sdc=$((n_sdc + 1))
    echo "  => SDC reproduced by pair '$trigger', moving on"
  else
    n_nosdc=$((n_nosdc + 1))
    echo "  => no SDC in $tried/$total pairs (errors: $errors)"
  fi
done

echo
echo "Done. Files processed: $n_files (SDC reproduced: $n_sdc, none: $n_nosdc), already done and skipped: $n_skipped"


