#!/usr/bin/env bash
#
# run_time_sweep.sh — run every shard produced by gen_time_sweep.py in parallel
# (one run_campaign.sh deterministic instance per shard_<n>of<m>.txt), wait for
# all of them, then print the per-bit timelines with analyze_time_sweep.py.
#
# Every pair is run (no stop at first SDC). Ctrl-C stops all instances.
#
# Can be launched from ANY directory: <sweep_dir>, --db and --schema are
# resolved against the directory you launch from, then the runs are executed
# from the repo root (the parent of this scripts/ folder), because
# run_campaign.sh uses repo-relative paths (tests/ocl_test/run_experiment.py,
# build/tests/opencl/ocl_test). It must still run where the simulator lives
# (i.e. inside the container).
#
# Usage:
#   <repo>/scripts/run_time_sweep.sh <sweep_dir> [--db <path> | <db_path>] [--schema <schema.sql>]
#
#   --db / 2nd argument: SQLite DB the runs are written to (created if missing).
#     Precedence: --db or 2nd argument > DB env var > <repo>/tests/ocl_test/time_sweep.db
#   --schema: SQL schema passed to run_experiment.py (--schema), used when it
#     has to create the tables of a new DB.
#     Precedence: --schema > SCHEMA env var > <repo>/tests/ocl_test/schema.sql
#
# Examples:
#   ./scripts/run_time_sweep.sh sweep_p1 --db tests/ocl_test/sweep_phase1.db
#   ./vortex/scripts/run_time_sweep.sh sweep_p1 --db sweep_phase1.db \
#       --schema vortex/tests/ocl_test/schema.sql
#
# Environment overrides:
#   REPO_ROOT  directory the runs are executed from (default: parent of scripts/)
#   ANALYZE    path to analyze_time_sweep.py (default: next to this script)
#
# Resume: pairs already present in the DB (same bit_index and cycle) are
# skipped, so an interrupted sweep can be relaunched with the same command.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(dirname "$SCRIPT_DIR")}"
RUN_CAMPAIGN="${RUN_CAMPAIGN:-$SCRIPT_DIR/run_campaign.sh}"
ANALYZE="${ANALYZE:-$SCRIPT_DIR/analyze_time_sweep.py}"

usage() { echo "Usage: $0 <sweep_dir> [--db <path> | <db_path>] [--schema <schema.sql>]" >&2; exit 1; }

SWEEP_DIR=""
DB_ARG=""
SCHEMA_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --db)        [ $# -ge 2 ] || usage; DB_ARG="$2"; shift 2 ;;
    --db=*)      DB_ARG="${1#--db=}"; shift ;;
    --schema)    [ $# -ge 2 ] || usage; SCHEMA_ARG="$2"; shift 2 ;;
    --schema=*)  SCHEMA_ARG="${1#--schema=}"; shift ;;
    -h|--help) usage ;;
    -*)      echo "Unknown option: $1" >&2; usage ;;
    *)
      if   [ -z "$SWEEP_DIR" ]; then SWEEP_DIR="$1"
      elif [ -z "$DB_ARG" ];    then DB_ARG="$1"
      else echo "Too many arguments: $1" >&2; usage
      fi
      shift ;;
  esac
done

[ -n "$SWEEP_DIR" ] && [ -d "$SWEEP_DIR" ] || usage
[ -x "$RUN_CAMPAIGN" ] || { echo "Not executable: $RUN_CAMPAIGN" >&2; exit 1; }

# Resolve everything to absolute paths BEFORE changing directory.
# User-given paths are relative to the launch directory; defaults to the repo.
abspath() { realpath -m -- "$1"; }
SWEEP_DIR="$(abspath "$SWEEP_DIR")"
if   [ -n "$DB_ARG" ];        then DB="$(abspath "$DB_ARG")"
elif [ -n "${DB:-}" ];        then DB="$(abspath "$DB")"
else                               DB="$REPO_ROOT/tests/ocl_test/time_sweep.db"
fi
if   [ -n "$SCHEMA_ARG" ];    then SCHEMA="$(abspath "$SCHEMA_ARG")"
elif [ -n "${SCHEMA:-}" ];    then SCHEMA="$(abspath "$SCHEMA")"
else                               SCHEMA="$REPO_ROOT/tests/ocl_test/schema.sql"
fi
export DB SCHEMA
mkdir -p "$(dirname "$DB")"

# run_campaign.sh needs these, relative to the repo root
if [ ! -f "$REPO_ROOT/tests/ocl_test/run_experiment.py" ]; then
  echo "run_experiment.py not found under $REPO_ROOT/tests/ocl_test/ (set REPO_ROOT?)" >&2
  exit 1
fi
if [ ! -d "$REPO_ROOT/build/tests/opencl/ocl_test" ]; then
  echo "Warning: $REPO_ROOT/build/tests/opencl/ocl_test not found; runs will fail" >&2
  echo "         unless you are where the simulator is built (inside the container?)" >&2
fi

# the schema is needed only to create a new DB, but a wrong path would make
# every run fail, so check it up front
if [ ! -f "$SCHEMA" ]; then
  if [ -f "$DB" ]; then
    echo "Warning: schema not found ($SCHEMA); using existing DB $DB" >&2
  else
    echo "Schema not found: $SCHEMA (needed to create $DB)" >&2
    exit 1
  fi
fi

shopt -s nullglob
shards=("$SWEEP_DIR"/shard_*of*.txt)
[ ${#shards[@]} -gt 0 ] || { echo "No shard_*of*.txt in $SWEEP_DIR" >&2; exit 1; }

# pairs already done (resume support); empty if the DB does not exist yet
DONE="$SWEEP_DIR/.done_pairs"
: > "$DONE"
if [ -f "$DB" ]; then
  python3 - "$DB" > "$DONE" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], timeout=60)
try:
    for cyc, bit in c.execute("SELECT i.cycle, i.bit_index FROM Injection i JOIN Run r ON r.run_id = i.run_id"):
        print(cyc, bit)
except sqlite3.Error:
    pass
EOF
fi

trap 'trap - INT TERM; echo; echo "Stopping all instances..."; kill 0' INT TERM

echo "Sweep: ${#shards[@]} shards"
echo "  repo   : $REPO_ROOT"
echo "  DB     : $DB"
echo "  schema : $SCHEMA"
echo "  logs   : $SWEEP_DIR"

pids=(); logs=(); todos=(); starts=()
for s in "${shards[@]}"; do
  base="$(basename "$s" .txt)"
  todo="$SWEEP_DIR/.${base}.todo"
  # keep only pairs not already in the DB
  # FILENAME test (not NR==FNR): the done-list may be empty
  awk 'FILENAME == ARGV[1] { done[$1" "$2]=1; next } NF>=2 && !(($1" "$2) in done)' "$DONE" "$s" > "$todo"
  n_todo=$(grep -c . "$todo" || true)
  log="$SWEEP_DIR/log_${base#shard_}.txt"
  echo "  $base: $n_todo runs to do -> $log"
  if [ "$n_todo" -gt 0 ]; then
    start_line=1
    [ -f "$log" ] && start_line=$(( $(wc -l < "$log") + 1 ))
    ( cd "$REPO_ROOT" && "$RUN_CAMPAIGN" deterministic "$todo" ) >> "$log" 2>&1 &
    pids+=($!); logs+=("$log"); todos+=("$n_todo"); starts+=("$start_line")
  fi
done

fail=0
for p in "${pids[@]}"; do wait "$p" || fail=1; done
echo
echo "All shards finished."

# run_campaign.sh stops a shard at the first failed run (|| break), so check
# how many runs each shard actually stored in THIS launch
problems=0
for i in "${!logs[@]}"; do
  log="${logs[$i]}"
  stored=$(tail -n +"${starts[$i]}" "$log" | grep -c 'Stored run' || true)
  if [ "$stored" -lt "${todos[$i]}" ]; then
    problems=1
    echo "WARNING: $(basename "$log"): stored $stored of ${todos[$i]} runs. Last lines:"
    tail -n +"${starts[$i]}" "$log" | tail -n 8 | sed 's/^/    | /'
  fi
done
if [ "$problems" -ne 0 ]; then
  echo "Some shards stopped early; fix the error above and relaunch the same command (it resumes)."
  fail=1
fi

if [ -f "$ANALYZE" ]; then
  python3 "$ANALYZE" --db "$DB" --manifest "$SWEEP_DIR/manifest.tsv" --csv "$SWEEP_DIR/results.csv"
else
  echo "Analyzer not found ($ANALYZE); run analyze_time_sweep.py by hand."
fi
exit $fail

