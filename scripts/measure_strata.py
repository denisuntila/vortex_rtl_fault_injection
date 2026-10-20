#!/usr/bin/env python3
"""
measure_strata.py — stratified measurement campaign, with and without sentinel.

For every stratum of strata.json (make_strata.py) and every kernel, runs the
stratum's number of runs; each run flips K bits (K = 1 for the clean strata),
drawn uniformly from the stratum (one range per run, chosen with probability
proportional to its size, then a uniform bit inside it), at K random even
timestamps. Campaign name in the DB: "strat:<kernel>:<stratum>:K<K>".
Resumable: only the missing runs are executed; relaunch to continue, or use a
larger --scale to add runs to every stratum.

Kernels (golden end timestamp - 100 = latest injection time):
  v1  ./ocl_sentinel -n16 -m16 -k16        (with sentinel)     max_ts 70098
  w0  ./ocl_sentinel -w0 -n16 -m16 -k16    (no sentinel lanes) max_ts 67764
  v1r ./ocl_sentinel -e 1 -n16 -m16 -k16   (sentinel + relaunch of the flagged warps)

Usage (repo root, inside the container):
  python3 scripts/measure_strata.py --strata strata.json --db tests/ocl_test/strata.db \\
      [--kernels v1,w0] [--scale 1.0] [--only sched_ctrl,issue_instr] [--instances N] [--dry-run]
"""

import argparse
import json
import math
import os
import random
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

KERNELS = {
    "v1": ("./ocl_sentinel -n16 -m16 -k16", 70098),
    "w0": ("./ocl_sentinel -w0 -n16 -m16 -k16", 67764),
    # with recovery: the warps whose sentinel fired are relaunched (once); faults
    # are injected only in the first launch (same window as v1)
    "v1r": ("./ocl_sentinel -e 1 -n16 -m16 -k16", 70098),
}
ENV_COMMON = ('LD_LIBRARY_PATH=/root/tools/pocl/lib:/workspace/build/runtime:/root/tools/llvm-vortex/lib: '
              'POCL_VORTEX_XLEN=32 LLVM_PREFIX=/root/tools/llvm-vortex '
              'POCL_VORTEX_BINTOOL="OBJCOPY=/root/tools/llvm-vortex/bin/llvm-objcopy /workspace/vortex/kernel/scripts/vxbin.py" '
              'POCL_VORTEX_CFLAGS="-march=rv32imaf -mabi=ilp32f -O3 -mcmodel=medany --sysroot=/root/tools/riscv32-gnu-toolchain/riscv32-unknown-elf '
              '--gcc-toolchain=/root/tools/riscv32-gnu-toolchain -fno-rtti -fno-exceptions -nostartfiles -nostdlib -fdata-sections '
              '-ffunction-sections -I/workspace/build/hw -I/workspace/vortex/kernel/include -DXLEN_32 -DNDEBUG  -Xclang -target-feature '
              '-Xclang +vortex -Xclang -target-feature -Xclang +zicond -mllvm -disable-loop-idiom-all      " '
              'POCL_VORTEX_LDFLAGS="-Wl,-Bstatic,--gc-sections,-T/workspace/vortex/kernel/scripts/link32.ld,--defsym=STARTUP_ADDR=0x80000000 '
              '/workspace/build/kernel/libvortex.a -L/root/tools/libc32/lib -lm -lc '
              '/root/tools/libcrt32/lib/baremetal/libclang_rt.builtins-riscv32.a" VORTEX_DRIVER=rtlsim')
CWD = "build/tests/opencl/ocl_test"


def counts(db):
    if not os.path.exists(db):
        return {}
    conn = sqlite3.connect(db, timeout=60)
    try:
        rows = conn.execute("""select c.test_name, count(*) from Run r join Campaign c on r.cid = c.id
                               where c.test_name like 'strat:%' group by 1""").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return dict(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strata", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--kernels", default="v1,w0")
    ap.add_argument("--scale", type=float, default=1.0, help="multiply every stratum's runs")
    ap.add_argument("--only", help="comma-separated strata to run (default: all)")
    ap.add_argument("--instances", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--schema", default="tests/ocl_test/schema.sql")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    spec = json.load(open(a.strata))
    strata = spec["strata"]
    if a.only:
        keep = set(a.only.split(","))
        strata = [s for s in strata if s["name"] in keep]
    kernels = a.kernels.split(",")
    for k in kernels:
        if k not in KERNELS:
            sys.exit(f"unknown kernel {k} (known: {', '.join(KERNELS)})")
    db = os.path.abspath(a.db)
    have = counts(db)
    rng = random.Random()

    jobs, plan = [], []
    for s in strata:
        target = math.ceil(s["runs"] * a.scale)
        for k in kernels:
            tname = f"strat:{k}:{s['name']}:K{s['K']}"
            need = max(0, target - have.get(tname, 0))
            plan.append((s["name"], k, s["K"], have.get(tname, 0), target))
            jobs.append([(s, k, tname)] * need)
    print(f"{'stratum':18s} {'kernel':6s} {'K':>3s} {'done':>6s} {'target':>6s}")
    for name, k, K, h, t in plan:
        print(f"{name:18s} {k:6s} {K:3d} {h:6d} {t:6d}")
    # round robin over (stratum, kernel) so that everything progresses together
    order = []
    while any(jobs):
        for j in jobs:
            if j:
                order.append(j.pop())
    print(f"{len(order)} runs to execute with {a.instances} instances")
    if a.dry_run or not order:
        return

    def run_one(s, k, tname):
        lo, hi = rng.choices(s["ranges"], weights=[h - l + 1 for l, h in s["ranges"]])[0]
        binary, max_ts = KERNELS[k]
        bits = f"{lo}-{hi}" if lo != hi else f"{lo}"
        cmd = (f"FAULT_MULTI_BITS={bits} FAULT_MULTI_COUNT={s['K']} FAULT_RANDOM_MAX_TIMESTAMP={max_ts} "
               f"{ENV_COMMON} {binary}")
        r = subprocess.run([sys.executable, "tests/ocl_test/run_experiment.py",
                            "--db", db, "--schema", a.schema, "--test-name", tname,
                            "--cores", "2", "--warps", "2", "--threads", "8",
                            "--cwd", CWD, "--timeout", str(a.timeout),
                            "--save-crash-log", "--save-masked-injections", "--command", cmd],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        return r.returncode, r.stdout[-300:]

    t0, fails = time.time(), 0
    with ThreadPoolExecutor(max_workers=a.instances) as ex:
        futs = [ex.submit(run_one, *j) for j in order]
        try:
            for i, fu in enumerate(as_completed(futs), 1):
                rc, tail = fu.result()
                if rc != 0:
                    fails += 1
                    print(f"   run_experiment.py exited {rc}: {tail.strip().splitlines()[-1] if tail.strip() else ''}")
                    if fails > 20 and fails > i // 10:
                        print("too many failed runs: stopping")
                        for f in futs:
                            f.cancel()
                        break
                if i % 50 == 0 or i == len(futs):
                    el = time.time() - t0
                    print(f"   {i}/{len(futs)} runs, {i / el * 3600:.0f} runs/h, "
                          f"about {(len(futs) - i) / (i / el) / 3600:.1f} h left", flush=True)
        except KeyboardInterrupt:
            print("\ninterrupted: cancelling pending runs (relaunch to resume)")
            for f in futs:
                f.cancel()
            raise


if __name__ == "__main__":
    main()

    