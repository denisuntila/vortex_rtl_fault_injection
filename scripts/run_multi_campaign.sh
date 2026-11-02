#!/usr/bin/env bash
#
# run_multi_campaign.sh — multi-injection campaign: for every target in a TSV,
# N runs, each flipping the target bit(s) K times at random even timestamps
# (processor.cpp FAULT_MULTI_* mode). Resumable: the runs already in the DB
# are counted per target and only the missing ones are executed, so
# relaunching continues an interrupted campaign, and relaunching with a
# larger N adds runs.
#
# Usage (from the repo root, inside the container):
#   ./scripts/run_multi_campaign.sh <db> <targets.tsv> <runs_per_target> [instances]
#
# targets.tsv, tab separated, '#' comments:
#   label   bits   K
#     bits = ruler index "B", or range "L-H" (one index drawn per fault)
#     K    = faults per run
# Every (label, K) pair is its own Campaign row: test_name = multi:<label>:K<K>.
#
# Environment:
#   MAX_TS      latest injection timestamp   (default 65370 = n16 kernel end - 100)
#   BINARY_CMD  test command                 (default ./ocl_test -n16 -m16 -k16)
#   TIMEOUT     seconds per run              (default 30)
#   SCHEMA      schema path                  (default tests/ocl_test/schema.sql)
#
# Logs: <db without .db>_logs/worker_<n>.log
# Stop: Ctrl-C (or pkill -f run_multi_campaign.sh), then relaunch to resume.

set -uo pipefail

DB="${1:-}"; TARGETS="${2:-}"; N="${3:-}"
M="${4:-$(( $(nproc) > 1 ? $(nproc) - 1 : 1 ))}"
[ -n "$DB" ] && [ -n "$TARGETS" ] && [ -n "$N" ] || {
  echo "Usage: $0 <db> <targets.tsv> <runs_per_target> [instances]" >&2; exit 1; }
[[ $N =~ ^[1-9][0-9]*$ && $M =~ ^[1-9][0-9]*$ ]] || {
  echo "runs_per_target and instances must be positive integers" >&2; exit 1; }
[ -f "$TARGETS" ] || { echo "targets file not found: $TARGETS" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DB="$(realpath -m "$DB")"
TARGETS="$(realpath "$TARGETS")"
SCHEMA="$(realpath -m "${SCHEMA:-$REPO_ROOT/tests/ocl_test/schema.sql}")"
cd "$REPO_ROOT" || exit 1

MAX_TS="${MAX_TS:-65370}"
BINARY_CMD="${BINARY_CMD:-./ocl_test -n16 -m16 -k16}"
TIMEOUT="${TIMEOUT:-30}"
CWD="build/tests/opencl/ocl_test"
CORES=2; WARPS=2; THREADS=8

# same environment as run_campaign.sh
ENV_COMMON='LD_LIBRARY_PATH=/root/tools/pocl/lib:/workspace/build/runtime:/root/tools/llvm-vortex/lib: POCL_VORTEX_XLEN=32 LLVM_PREFIX=/root/tools/llvm-vortex POCL_VORTEX_BINTOOL="OBJCOPY=/root/tools/llvm-vortex/bin/llvm-objcopy /workspace/vortex/kernel/scripts/vxbin.py" POCL_VORTEX_CFLAGS="-march=rv32imaf -mabi=ilp32f -O3 -mcmodel=medany --sysroot=/root/tools/riscv32-gnu-toolchain/riscv32-unknown-elf --gcc-toolchain=/root/tools/riscv32-gnu-toolchain -fno-rtti -fno-exceptions -nostartfiles -nostdlib -fdata-sections -ffunction-sections -I/workspace/build/hw -I/workspace/vortex/kernel/include -DXLEN_32 -DNDEBUG  -Xclang -target-feature -Xclang +vortex -Xclang -target-feature -Xclang +zicond -mllvm -disable-loop-idiom-all      " POCL_VORTEX_LDFLAGS="-Wl,-Bstatic,--gc-sections,-T/workspace/vortex/kernel/scripts/link32.ld,--defsym=STARTUP_ADDR=0x80000000 /workspace/build/kernel/libvortex.a -L/root/tools/libc32/lib -lm -lc /root/tools/libcrt32/lib/baremetal/libclang_rt.builtins-riscv32.a" VORTEX_DRIVER=rtlsim'

[ -x "$CWD/${BINARY_CMD%% *}" ] || { echo "$CWD/${BINARY_CMD%% *} not found: build ocl_test first" >&2; exit 1; }

mkdir -p "$(dirname "$DB")"
LOGDIR="${DB%.db}_logs"
mkdir -p "$LOGDIR"
JOBS="$LOGDIR/jobs.tsv"

count_runs() {  # count_runs <test_name>
  [ -f "$DB" ] || { echo 0; return; }
  sqlite3 -cmd '.timeout 60000' "$DB" \
    "select count(*) from Run r join Campaign c on r.cid = c.id where c.test_name = '$1'" 2>/dev/null || echo 0
}

# ---- read the targets and count what is missing ----
labels=(); bitss=(); ks=(); needs=()
maxneed=0
echo "Multi-injection campaign: $N runs per target, $M instances, timestamps in [150, $MAX_TS]"
echo "  DB: $DB | command: $BINARY_CMD | timeout: $TIMEOUT s"
while IFS=$'\t' read -r label bits k _; do
  [[ -z "${label// }" || $label == \#* ]] && continue
  [[ $bits =~ ^[0-9]+(-[0-9]+)?$ && $k =~ ^[1-9][0-9]*$ ]] || {
    echo "bad target line: '$label' '$bits' '$k' (want: label<TAB>B or L-H<TAB>K)" >&2; exit 1; }
  tname="multi:${label}:K${k}"
  have=$(count_runs "$tname")
  need=$(( N - have > 0 ? N - have : 0 ))
  printf '  %-28s bits %-15s K=%-3s %5d/%d done\n' "$label" "$bits" "$k" "$have" "$N"
  labels+=("$label"); bitss+=("$bits"); ks+=("$k"); needs+=("$need")
  (( need > maxneed )) && maxneed=$need
done < "$TARGETS"

# ---- job list, round-robin over the targets so they all progress together ----
: > "$JOBS"
for ((r = 0; r < maxneed; r++)); do
  for i in "${!labels[@]}"; do
    (( r < needs[i] )) && printf '%s\t%s\t%s\n' "${labels[i]}" "${bitss[i]}" "${ks[i]}" >> "$JOBS"
  done
done
TOTAL=$(wc -l < "$JOBS")
[ "$TOTAL" -eq 0 ] && { echo "Nothing to do: every target has $N runs."; exit 0; }
echo "  $TOTAL runs to execute"

trap 'trap - INT TERM; echo; echo "Stopping all workers..."; kill 0' INT TERM

worker() {  # worker <n>: runs jobs n, n+M, n+2M, ...
  local n=$1 done_=0 label bits k
  while IFS=$'\t' read -r label bits k; do
    python3 tests/ocl_test/run_experiment.py \
      --db "$DB" --schema "$SCHEMA" \
      --test-name "multi:${label}:K${k}" \
      --cores "$CORES" --warps "$WARPS" --threads "$THREADS" \
      --cwd "$CWD" --timeout "$TIMEOUT" \
      --save-crash-log --save-masked-injections \
      --command "FAULT_MULTI_BITS=${bits} FAULT_MULTI_COUNT=${k} FAULT_RANDOM_MAX_TIMESTAMP=${MAX_TS} ${ENV_COMMON} ${BINARY_CMD}"
    rc=$?
    if [ $rc -ne 0 ]; then
      echo "worker $n: run_experiment.py exited with $rc on target $label, stopping this worker"
      return 1
    fi
    done_=$((done_ + 1))
  done < <(awk -v n="$n" -v m="$M" '(NR - 1) % m == n' "$JOBS")
  echo "worker $n: finished ($done_ runs)"
}

for ((n = 0; n < M; n++)); do
  worker "$n" > "$LOGDIR/worker_$n.log" 2>&1 &   # overwritten: logs of this launch only
  sleep 2
done
wait

echo
echo "Done. Runs per target:"
sqlite3 -cmd '.timeout 60000' "$DB" \
  "select c.test_name, count(*), sum(r.outcome='masked_error'), sum(r.outcome='sdc'), sum(r.outcome='crash'), sum(r.outcome='timeout')
   from Run r join Campaign c on r.cid = c.id where c.test_name like 'multi:%' group by 1 order by 1" \
  | awk -F'|' 'BEGIN{printf "  %-36s %6s %7s %5s %6s %8s\n","test_name","runs","masked","sdc","crash","timeout"}
               {printf "  %-36s %6d %7d %5d %6d %8d\n",$1,$2,$3,$4,$5,$6}'
grep -h 'stopping this worker' "$LOGDIR"/worker_*.log && echo "WARNING: some workers stopped early (see $LOGDIR)"
exit 0
