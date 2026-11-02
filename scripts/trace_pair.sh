#!/usr/bin/env bash
#
# trace_pair.sh — run ocl_test twice with the warp-control trace enabled:
# once fault-free (golden, cached) and once with a single deterministic fault,
# then compare the two traces with trace_diff.py.
#
# Usage (from the repo root, inside the container):
#   ./scripts/trace_pair.sh <timestamp> <bit_index> [out_dir]
#   ./scripts/trace_pair.sh golden [out_dir]        # only the golden run (+ wall time)
#
# Environment:
#   BINARY_CMD   test command (default: ./ocl_test -n16 -m16 -k16)
#                With a non-default command the default out_dir becomes
#                tests/ocl_test/traces_<command sanitized>, so goldens of
#                different problem sizes never mix.
#
# Output: <out_dir>/golden.txt, <out_dir>/fault_<ts>_<bit>.txt, the program
# output of both runs (.log) and the diff printed on screen.
# Uses the same environment as run_campaign.sh.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
CWD="$REPO_ROOT/build/tests/opencl/ocl_test"

DEFAULT_CMD='./ocl_test -n16 -m16 -k16'
BINARY_CMD="${BINARY_CMD:-$DEFAULT_CMD}"
if [ "$BINARY_CMD" = "$DEFAULT_CMD" ]; then
  DEFAULT_OUT="tests/ocl_test/traces"
else
  DEFAULT_OUT="tests/ocl_test/traces_$(echo "${BINARY_CMD#./}" | tr -c 'a-zA-Z0-9\n' '_')"
fi

if [ "${1:-}" = "golden" ]; then
  MODE=golden; TS=""; BIT=""; OUT="${2:-$DEFAULT_OUT}"
else
  MODE=pair; TS="${1:-}"; BIT="${2:-}"; OUT="${3:-$DEFAULT_OUT}"
  [ -n "$TS" ] && [ -n "$BIT" ] || {
    echo "Usage: $0 <timestamp> <bit_index> [out_dir] | $0 golden [out_dir]" >&2; exit 1; }
fi

# ---- same env as run_campaign.sh ----
ENV_COMMON='LD_LIBRARY_PATH=/root/tools/pocl/lib:/workspace/build/runtime:/root/tools/llvm-vortex/lib: POCL_VORTEX_XLEN=32 LLVM_PREFIX=/root/tools/llvm-vortex POCL_VORTEX_BINTOOL="OBJCOPY=/root/tools/llvm-vortex/bin/llvm-objcopy /workspace/vortex/kernel/scripts/vxbin.py" POCL_VORTEX_CFLAGS="-march=rv32imaf -mabi=ilp32f -O3 -mcmodel=medany --sysroot=/root/tools/riscv32-gnu-toolchain/riscv32-unknown-elf --gcc-toolchain=/root/tools/riscv32-gnu-toolchain -fno-rtti -fno-exceptions -nostartfiles -nostdlib -fdata-sections -ffunction-sections -I/workspace/build/hw -I/workspace/vortex/kernel/include -DXLEN_32 -DNDEBUG  -Xclang -target-feature -Xclang +vortex -Xclang -target-feature -Xclang +zicond -mllvm -disable-loop-idiom-all      " POCL_VORTEX_LDFLAGS="-Wl,-Bstatic,--gc-sections,-T/workspace/vortex/kernel/scripts/link32.ld,--defsym=STARTUP_ADDR=0x80000000 /workspace/build/kernel/libvortex.a -L/root/tools/libc32/lib -lm -lc /root/tools/libcrt32/lib/baremetal/libclang_rt.builtins-riscv32.a" VORTEX_DRIVER=rtlsim'

mkdir -p "$OUT"
OUT="$(realpath "$OUT")"
GOLD="$OUT/golden.txt"

run() {  # run <fault env> <trace file> <log file>
  local t0=$SECONDS
  ( cd "$CWD" && bash -c "$1 FAULT_TRACE_FILE=$2 ${ENV_COMMON} ${BINARY_CMD}" ) > "$3" 2>&1
  local rc=$?
  echo "  exit code $rc, wall time $((SECONDS - t0)) s, $(grep -m1 -o 'kernel end timestamp=[0-9]*' "$3" || echo 'no end timestamp')" \
       "| $(grep -m1 -oE '"status": *"[a-z_]+"' "$3" || echo 'no status line')"
}

echo "command: $BINARY_CMD  | out: $OUT"

# golden: FAULT_TIMESTAMP far beyond the end, so nothing is injected
# (without FAULT_* the default continuous random schedule would inject)
if [ "$MODE" = golden ] || [ ! -s "$GOLD" ]; then
  echo "golden run..."
  run "FAULT_TIMESTAMP=100000000000000 FAULT_BIT_INDEX=0" "$GOLD" "$OUT/golden.log"
else
  echo "golden trace cached: $GOLD"
fi
[ "$MODE" = golden ] && exit 0

FAULT="$OUT/fault_${TS}_${BIT}.txt"
echo "faulty run (ts=$TS bit=$BIT)..."
run "FAULT_TIMESTAMP=$TS FAULT_BIT_INDEX=$BIT" "$FAULT" "$OUT/fault_${TS}_${BIT}.log"

python3 "$SCRIPT_DIR/trace_diff.py" "$GOLD" "$FAULT" --inject-ts "$TS"

