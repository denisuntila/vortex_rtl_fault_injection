#!/usr/bin/env bash
#
# run_campaign.sh — run many ocl_test fault-injection experiments in a row.
#
# Usage:
#   ./run_campaign.sh default [num_runs]
#   ./run_campaign.sh random <FAULT_RANDOM_MAX_TIMESTAMP> [num_runs]
#   ./run_campaign.sh deterministic <pairs_file>
#
#   default        - unchanged continuous random fault schedule (no FAULT_* vars)
#   random         - one random fault per run, timestamp drawn uniformly from
#                    [150, FAULT_RANDOM_MAX_TIMESTAMP], random dart
#   deterministic  - one run per (timestamp, bit_index) pair read from
#                    <pairs_file>, one pair per line, e.g.:
#                        340 882121
#                        512 190442
#                    (useful for re-verifying specific previously-found faults;
#                    running the same pair repeatedly gives the same result
#                    every time, so this mode sweeps a list instead of looping
#                    on one fixed pair)
#
# Examples:
#   ./run_campaign.sh default
#   ./run_campaign.sh default 200
#   ./run_campaign.sh random 5000
#   ./run_campaign.sh random 5000 200
#   ./run_campaign.sh deterministic faults_to_recheck.txt

set -uo pipefail

# ---- Fixed settings shared by every run ----
NUM_RUNS_DEFAULT=500
DB="tests/ocl_test/campaign.db"
SCHEMA="tests/ocl_test/schema.sql"
TEST_NAME="sgemm_seu"
CORES=2
WARPS=2
THREADS=8
CWD="build/tests/opencl/ocl_test"
TIMEOUT=30

# ---- Env vars + binary invocation, unchanged from the original command ----
ENV_COMMON='LD_LIBRARY_PATH=/root/tools/pocl/lib:/workspace/build/runtime:/root/tools/llvm-vortex/lib: POCL_VORTEX_XLEN=32 LLVM_PREFIX=/root/tools/llvm-vortex POCL_VORTEX_BINTOOL="OBJCOPY=/root/tools/llvm-vortex/bin/llvm-objcopy /workspace/vortex/kernel/scripts/vxbin.py" POCL_VORTEX_CFLAGS="-march=rv32imaf -mabi=ilp32f -O3 -mcmodel=medany --sysroot=/root/tools/riscv32-gnu-toolchain/riscv32-unknown-elf --gcc-toolchain=/root/tools/riscv32-gnu-toolchain -fno-rtti -fno-exceptions -nostartfiles -nostdlib -fdata-sections -ffunction-sections -I/workspace/build/hw -I/workspace/vortex/kernel/include -DXLEN_32 -DNDEBUG  -Xclang -target-feature -Xclang +vortex -Xclang -target-feature -Xclang +zicond -mllvm -disable-loop-idiom-all      " POCL_VORTEX_LDFLAGS="-Wl,-Bstatic,--gc-sections,-T/workspace/vortex/kernel/scripts/link32.ld,--defsym=STARTUP_ADDR=0x80000000 /workspace/build/kernel/libvortex.a -L/root/tools/libc32/lib -lm -lc /root/tools/libcrt32/lib/baremetal/libclang_rt.builtins-riscv32.a" VORTEX_DRIVER=rtlsim'
BINARY_CMD='./ocl_test -n16 -m16 -k16'

# run_one <fault_env_prefix> <run_index> <total_runs>
# fault_env_prefix must already include a trailing space if non-empty,
# e.g. "FAULT_RANDOM_MAX_TIMESTAMP=5000 "
run_one() {
  local fault_env="$1"
  local i="$2"
  local total="$3"

  echo "=== Run $i/$total ==="
  python3 tests/ocl_test/run_experiment.py \
    --db "$DB" \
    --schema "$SCHEMA" \
    --test-name "$TEST_NAME" \
    --cores "$CORES" --warps "$WARPS" --threads "$THREADS" \
    --cwd "$CWD" \
    --timeout "$TIMEOUT" \
    --save-crash-log \
    --save-masked-injections \
    --command "${fault_env}${ENV_COMMON} ${BINARY_CMD}"
}

MODE="${1:-}"

case "$MODE" in

  default)
    NUM_RUNS="${2:-$NUM_RUNS_DEFAULT}"
    for ((i = 1; i <= NUM_RUNS; i++)); do
      run_one "" "$i" "$NUM_RUNS" || break
    done
    ;;

  random)
    if [ -z "${2:-}" ]; then
      echo "Usage: $0 random <FAULT_RANDOM_MAX_TIMESTAMP> [num_runs]" >&2
      exit 1
    fi
    MAX_TS="$2"
    NUM_RUNS="${3:-$NUM_RUNS_DEFAULT}"
    for ((i = 1; i <= NUM_RUNS; i++)); do
      run_one "FAULT_RANDOM_MAX_TIMESTAMP=${MAX_TS} " "$i" "$NUM_RUNS" || break
    done
    ;;

  deterministic)
    PAIRS_FILE="${2:-}"
    if [ -z "$PAIRS_FILE" ]; then
      echo "Usage: $0 deterministic <pairs_file>   (one 'timestamp bit_index' pair per line)" >&2
      exit 1
    fi
    if [ ! -f "$PAIRS_FILE" ]; then
      echo "Pairs file not found: $PAIRS_FILE" >&2
      exit 1
    fi

    TOTAL=$(grep -cve '^[[:space:]]*$' "$PAIRS_FILE")
    i=0
    while read -r TS BIT _; do
      [ -z "$TS" ] && continue
      i=$((i + 1))
      run_one "FAULT_TIMESTAMP=${TS} FAULT_BIT_INDEX=${BIT} " "$i" "$TOTAL" || break
    done < "$PAIRS_FILE"
    ;;

  *)
    echo "Usage:"
    echo "  $0 default [num_runs]"
    echo "  $0 random <FAULT_RANDOM_MAX_TIMESTAMP> [num_runs]"
    echo "  $0 deterministic <pairs_file>"
    exit 1
    ;;
esac