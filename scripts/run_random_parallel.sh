#!/usr/bin/env bash
#
# run_random_parallel.sh — statistical campaign: M parallel instances of
#   run_campaign.sh random <MAX_TS> <RUNS_PER_INSTANCE>
# all writing to the same DB (one uniform random fault per run: random
# timestamp in [150, MAX_TS], random bit over the whole ruler).
#
# Usage (from the repo root, inside the container):
#   ./scripts/run_random_parallel.sh <db> <max_ts> <runs_per_instance> [instances]
#
# Environment (forwarded to run_campaign.sh):
#   BINARY_CMD   test command      (default in run_campaign.sh: ./ocl_test -n16 -m16 -k16)
#   TEST_NAME    Campaign.test_name (use a different name per problem size)
#   TIMEOUT      seconds per run    (scale it with the problem size)
#   SCHEMA       schema path        (default tests/ocl_test/schema.sql)
#
# Logs: <db without .db>_logs/inst_<n>.log. Progress:
#   sqlite3 <db> "select outcome, count(*) from Run group by 1"
# Relaunching simply adds more runs (random mode has nothing to resume).
# Ctrl-C stops every instance.

set -uo pipefail

DB="${1:-}"; MAX_TS="${2:-}"; PER="${3:-}"; M="${4:-$(nproc)}"
[ -n "$DB" ] && [ -n "$MAX_TS" ] && [ -n "$PER" ] || {
  echo "Usage: $0 <db> <max_ts> <runs_per_instance> [instances]" >&2; exit 1; }
[[ $MAX_TS =~ ^[0-9]+$ && $PER =~ ^[0-9]+$ && $M =~ ^[1-9][0-9]*$ ]] || {
  echo "max_ts, runs_per_instance, instances must be positive integers" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

export DB="$(realpath -m "$DB")"
export SCHEMA="$(realpath -m "${SCHEMA:-$REPO_ROOT/tests/ocl_test/schema.sql}")"
[ -n "${BINARY_CMD:-}" ] && export BINARY_CMD
[ -n "${TEST_NAME:-}" ] && export TEST_NAME
[ -n "${TIMEOUT:-}" ] && export TIMEOUT
mkdir -p "$(dirname "$DB")"
LOGDIR="${DB%.db}_logs"
mkdir -p "$LOGDIR"

trap 'trap - INT TERM; echo; echo "Stopping all instances..."; kill 0' INT TERM

echo "Random campaign: $M instances x $PER runs, timestamps in [150, $MAX_TS]"
echo "  DB       : $DB"
echo "  command  : ${BINARY_CMD:-(run_campaign.sh default)}"
echo "  test name: ${TEST_NAME:-(run_campaign.sh default)} | timeout: ${TIMEOUT:-(default)} s"
echo "  logs     : $LOGDIR"

pids=()
for ((n = 0; n < M; n++)); do
  ( cd "$REPO_ROOT" && ./scripts/run_campaign.sh random "$MAX_TS" "$PER" ) >> "$LOGDIR/inst_$n.log" 2>&1 &
  pids+=($!)
  sleep 2   # stagger the starts (pocl compilation, DB creation)
done
for p in "${pids[@]}"; do wait "$p"; done

echo
echo "All instances finished."
for ((n = 0; n < M; n++)); do
  stored=$(grep -c 'Stored run' "$LOGDIR/inst_$n.log" || true)
  [ "$stored" -lt "$PER" ] && { echo "WARNING: instance $n stored $stored of $PER runs. Last lines:"; tail -n 6 "$LOGDIR/inst_$n.log" | sed 's/^/    | /'; }
done
sqlite3 "$DB" "select outcome, count(*) from Run group by 1" 2>/dev/null || true

