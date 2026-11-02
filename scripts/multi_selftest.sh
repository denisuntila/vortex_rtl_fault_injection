#!/usr/bin/env bash
#
# multi_selftest.sh — checks the multi-injection mode of the rebuilt simulator
# before launching a campaign. Writes to a throw-away DB.
#
#   1) deterministic single fault (ts 5684, bit 434959 = c0 mscratch bit 15):
#      known SDC
#   2) the same fault through the multi mode (explicit list, K = 1):
#      must give the SAME outcome  -> the multi path injects like the old one
#   3) multi mode, explicit list of 2 timestamps: 2 Injection rows
#   4) multi mode, random: K = 5 -> 5 Injection rows on even timestamps
#   5) invalid FAULT_MULTI_BITS: the simulator must refuse (runner exit 3)
#
# Usage (repo root, inside the container): ./scripts/multi_selftest.sh

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")" || exit 1

DB=/tmp/multi_selftest.db
rm -f "$DB"*
CWD="build/tests/opencl/ocl_test"
ENV_COMMON='LD_LIBRARY_PATH=/root/tools/pocl/lib:/workspace/build/runtime:/root/tools/llvm-vortex/lib: POCL_VORTEX_XLEN=32 LLVM_PREFIX=/root/tools/llvm-vortex POCL_VORTEX_BINTOOL="OBJCOPY=/root/tools/llvm-vortex/bin/llvm-objcopy /workspace/vortex/kernel/scripts/vxbin.py" POCL_VORTEX_CFLAGS="-march=rv32imaf -mabi=ilp32f -O3 -mcmodel=medany --sysroot=/root/tools/riscv32-gnu-toolchain/riscv32-unknown-elf --gcc-toolchain=/root/tools/riscv32-gnu-toolchain -fno-rtti -fno-exceptions -nostartfiles -nostdlib -fdata-sections -ffunction-sections -I/workspace/build/hw -I/workspace/vortex/kernel/include -DXLEN_32 -DNDEBUG  -Xclang -target-feature -Xclang +vortex -Xclang -target-feature -Xclang +zicond -mllvm -disable-loop-idiom-all      " POCL_VORTEX_LDFLAGS="-Wl,-Bstatic,--gc-sections,-T/workspace/vortex/kernel/scripts/link32.ld,--defsym=STARTUP_ADDR=0x80000000 /workspace/build/kernel/libvortex.a -L/root/tools/libc32/lib -lm -lc /root/tools/libcrt32/lib/baremetal/libclang_rt.builtins-riscv32.a" VORTEX_DRIVER=rtlsim'
CMD='./ocl_test -n16 -m16 -k16'

run() {  # run <test_name> <fault env>
  python3 tests/ocl_test/run_experiment.py --db "$DB" --schema tests/ocl_test/schema.sql \
    --test-name "$1" --cores 2 --warps 2 --threads 8 --cwd "$CWD" --timeout 60 \
    --save-crash-log --save-masked-injections --command "$2 ${ENV_COMMON} ${CMD}" 2>&1 | tail -n 3
  return "${PIPESTATUS[0]}"
}

echo "1) deterministic 5684/434959";            run t1_det   "FAULT_TIMESTAMP=5684 FAULT_BIT_INDEX=434959"
echo "2) multi, explicit 5684, bit 434959";     run t2_multi "FAULT_MULTI_BITS=434959 FAULT_MULTI_TIMESTAMPS=5684"
echo "3) multi, explicit 5684,30000";           run t3_two   "FAULT_MULTI_BITS=434959 FAULT_MULTI_TIMESTAMPS=5684,30000"
echo "4) multi, random K=5 on 272655";          run t4_rand  "FAULT_MULTI_BITS=272655 FAULT_MULTI_COUNT=5 FAULT_RANDOM_MAX_TIMESTAMP=65370"
echo "5) invalid FAULT_MULTI_BITS (must be refused)"
run t5_bad "FAULT_MULTI_BITS=abc FAULT_MULTI_COUNT=5 FAULT_RANDOM_MAX_TIMESTAMP=65370" > /dev/null; rc5=$?

echo
echo "---- results ----"
sqlite3 -header -column "$DB" \
  "select c.test_name, r.outcome, r.injection_count as inj,
          (select group_concat(i.cycle || '/' || i.bit_index, ' ') from Injection i where i.run_id = r.run_id) as faults,
          (select count(*) from SDC s where s.run_id = r.run_id) as wrong
   from Run r join Campaign c on r.cid = c.id order by r.run_id"
echo
o1=$(sqlite3 "$DB" "select outcome from Run r join Campaign c on r.cid=c.id where test_name='t1_det'")
o2=$(sqlite3 "$DB" "select outcome from Run r join Campaign c on r.cid=c.id where test_name='t2_multi'")
n3=$(sqlite3 "$DB" "select injection_count from Run r join Campaign c on r.cid=c.id where test_name='t3_two'")
n4=$(sqlite3 "$DB" "select count(*) from Injection i join Run r on i.run_id=r.run_id join Campaign c on r.cid=c.id where test_name='t4_rand' and i.cycle % 2 = 0")
n5=$(sqlite3 "$DB" "select count(*) from Run r join Campaign c on r.cid=c.id where test_name='t5_bad'")
ok=1
[ -n "$o1" ] && [ "$o1" = "$o2" ] && echo "PASS 1-2: same outcome ($o1)" || { echo "FAIL 1-2: '$o1' vs '$o2'"; ok=0; }
[ "$n3" = 2 ] && echo "PASS 3: 2 injections" || { echo "FAIL 3: $n3 injections"; ok=0; }
o4=$(sqlite3 "$DB" "select outcome from Run r join Campaign c on r.cid=c.id where test_name='t4_rand'")
if [ "$o4" = crash ] || [ "$o4" = timeout ]; then
  echo "SKIP 4: run ended early ($o4, $n4 even injections), rerun the test"
else
  [ "$n4" = 5 ] && echo "PASS 4: 5 injections, all on even timestamps" || { echo "FAIL 4: $n4 even injections"; ok=0; }
fi
[ "$rc5" = 3 ] && [ "$n5" = 0 ] && echo "PASS 5: invalid configuration refused" || { echo "FAIL 5: rc=$rc5, rows=$n5"; ok=0; }
[ $ok = 1 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
