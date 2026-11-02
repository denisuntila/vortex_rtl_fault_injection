#!/usr/bin/env python3
"""
discover.py — hierarchical search of the ruler for the parts that make the
kernel fail, with multi-injection (FAULT_MULTI_BITS=L-H, one range of the node per run,
chosen with probability proportional to its size: no simulator rebuild needed).

Starting from a node of ruler_tree.json (default: the root), every node of the
current level gets R runs; each run flips K bits drawn uniformly from the
node's ranges, at K random even timestamps. A node with at least one
non-masked run (or one SDC, with --split-on sdc) is expanded into its
children; the others are closed as "not vulnerable at this budget". The
search ends at the signals (leaves) or at --max-depth.

  * nodes with one child that covers all their bits are skipped (no runs);
  * K is reduced on small nodes, K_eff = min(K, ceil(bits / --bits-per-fault)),
    because flipping the same bit twice can undo the first flip, and R is
    raised accordingly, R_eff = min(--max-runs, ceil(R * K / K_eff)), so every
    node gets about R*K faults;
  * resumable: each node is a campaign "disc:<node>:K<k>" in the DB and only
    the missing runs are executed; relaunching continues the search.

With R*K faults per node, a node whose per-fault failure probability is a is
missed with probability (1 - a)^(R*K): e.g. 500 faults miss a = 0.6% with
probability 5%.

Usage (repo root, inside the container):
  python3 scripts/discover.py --tree ruler_tree.json --db tests/ocl_test/discover.db \\
      [--node NODE] [--runs 25] [--K 20] [--instances 7] [--max-depth 99] \\
      [--binary './ocl_sentinel -n16 -m16 -k16'] [--max-ts 70098] [--timeout 60] [--dry-run]
Output: progress on stdout, and <db>_report.tsv (one line per visited node).
"""

import argparse
import math
import os
import random
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def pretty(p):
    return "/".join(c.replace("__BRA__", "[").replace("__KET__", "]").replace("__05F", "_") for c in p.split("/"))


def resolve(tree, name):
    if name is None:
        if len(tree["roots"]) != 1:
            sys.exit("several roots: pass --node")
        return tree["roots"][0]
    if name in tree["nodes"]:
        return name
    for p in tree["nodes"]:
        if pretty(p) == name:
            return p
    sys.exit(f"node not found: {name}")


def descend(tree, p):
    """skip nodes with a single child holding all their bits (same fault set)"""
    while True:
        n = tree["nodes"][p]
        if len(n["children"]) == 1 and tree["nodes"][n["children"][0]]["bits"] == n["bits"]:
            p = n["children"][0]
        else:
            return p


def subtract(ranges, holes):
    """ranges minus holes (both lists of [lo, hi], holes sorted and merged)"""
    out = []
    for lo, hi in ranges:
        cur = lo
        for hl, hh in holes:
            if hh < cur or hl > hi:
                continue
            if hl > cur:
                out.append([cur, hl - 1])
            cur = max(cur, hh + 1)
            if cur > hi:
                break
        if cur <= hi:
            out.append([cur, hi])
    return out


def merge(ranges):
    out = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def expand(tree, p, min_split):
    """children to visit: with min_split > 0, the children larger than min_split
    each on its own, and all the smaller ones together in one "<p>/(rest)" node;
    [] if there is no child larger than min_split (then p itself is a stratum)"""
    kids = [descend(tree, ch) for ch in tree["nodes"][p]["children"]]
    if min_split <= 0:
        return kids
    big = [ch for ch in kids if tree["nodes"][ch]["bits"] > min_split]
    if not big:
        return []
    rest = subtract(tree["nodes"][p]["ranges"], merge([r for ch in big for r in tree["nodes"][ch]["ranges"]]))
    if rest:
        rp = p + "/(rest)"
        tree["nodes"][rp] = {"bits": sum(h - l + 1 for l, h in rest), "ranges": rest, "leaf": True,
                             "children": [], "nsig": len(kids) - len(big), "pseudo": True}
        big.append(rp)
    return big


def counts(db, tname):
    if not os.path.exists(db):
        return {}
    conn = sqlite3.connect(db, timeout=60)
    try:
        rows = conn.execute("""select r.outcome, count(*) from Run r join Campaign c on r.cid = c.id
                               where c.test_name = ? group by 1""", (tname,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return dict(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tree", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--node", help="start node (default: the root)")
    ap.add_argument("--runs", type=int, default=25, help="R: runs per node at full K")
    ap.add_argument("--K", type=int, default=20, help="faults per run on large nodes")
    ap.add_argument("--bits-per-fault", type=int, default=8, help="K_eff = min(K, ceil(bits / this))")
    ap.add_argument("--max-runs", type=int, default=100, help="cap on runs per node")
    ap.add_argument("--split-on", choices=("any", "sdc"), default="any",
                    help="expand a node on any non-masked run (default) or only on SDC")
    ap.add_argument("--max-depth", type=int, default=99, help="levels below the start node")
    ap.add_argument("--min-split-bits", type=int, default=0,
                    help="with N > 0: vulnerable nodes of at most N bits are not expanded (they become strata),\n"
                         "and when a node is expanded its children of at most N bits are tested together\n"
                         "as one '<node>/(rest)' node; this bounds the search to the large nodes")
    ap.add_argument("--instances", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--binary", default="./ocl_sentinel -n16 -m16 -k16")
    ap.add_argument("--max-ts", type=int, default=70098)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--schema", default="tests/ocl_test/schema.sql")
    ap.add_argument("--dry-run", action="store_true", help="show the first level and stop")
    a = ap.parse_args()

    import json
    with open(a.tree) as f:
        tree = json.load(f)
    db = os.path.abspath(a.db)
    report_path = (db[:-3] if db.endswith(".db") else db) + "_report.tsv"

    def plan(p):
        bits = tree["nodes"][p]["bits"]
        k = max(1, min(a.K, math.ceil(bits / a.bits_per_fault)))
        r = min(a.max_runs, math.ceil(a.runs * a.K / k))
        return k, r

    rng = random.Random()

    def pick_range(p):
        """one range of the node, chosen with probability proportional to its size:
        with K = 1 the bit is uniform over the whole node; with K > 1 the K faults
        of a run fall in the same range (fine for finding vulnerable parts).
        Works with the current simulator (FAULT_MULTI_BITS=L-H): no rebuild."""
        rs = tree["nodes"][p]["ranges"]
        lo, hi = rng.choices(rs, weights=[h - l + 1 for l, h in rs])[0]
        return f"{lo}-{hi}" if lo != hi else f"{lo}"

    def run_one(p, k):
        cmd = (f"FAULT_MULTI_BITS={pick_range(p)} FAULT_MULTI_COUNT={k} "
               f"FAULT_RANDOM_MAX_TIMESTAMP={a.max_ts} {ENV_COMMON} {a.binary}")
        r = subprocess.run([sys.executable, "tests/ocl_test/run_experiment.py",
                            "--db", db, "--schema", a.schema, "--test-name", f"disc:{p}:K{k}",
                            "--cores", "2", "--warps", "2", "--threads", "8",
                            "--cwd", CWD, "--timeout", str(a.timeout),
                            "--save-crash-log", "--save-masked-injections", "--command", cmd],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        return r.returncode, r.stdout[-400:]

    start = descend(tree, resolve(tree, a.node))
    total = tree["total_bits"]
    level, depth, visited = [start], 0, []
    t0 = time.time()
    done_runs = 0
    while level and depth <= a.max_depth:
        todo = []
        for p in level:
            k, r = plan(p)
            have = sum(counts(db, f"disc:{p}:K{k}").values())
            todo.extend([(p, k)] * max(0, r - have))
        print(f"\n== level {depth}: {len(level)} nodes, {len(todo)} runs to execute")
        for p in level[:30]:
            k, r = plan(p)
            print(f"   {tree['nodes'][p]['bits']:9d} bits  K={k:<3d} R={r:<4d} {pretty(p)}")
        if len(level) > 30:
            print(f"   ... and {len(level) - 30} more")
        if a.dry_run:
            return
        # round-robin order so that all nodes progress together
        order, buckets = [], {}
        for p, k in todo:
            buckets.setdefault(p, []).append((p, k))
        while buckets:
            for p in list(buckets):
                order.append(buckets[p].pop())
                if not buckets[p]:
                    del buckets[p]
        failures = 0
        with ThreadPoolExecutor(max_workers=a.instances) as ex:
            futs = [ex.submit(run_one, p, k) for p, k in order]
            try:
                for i, fu in enumerate(as_completed(futs), 1):
                    rc, tail = fu.result()
                    done_runs += 1
                    if rc != 0:
                        failures += 1
                        print(f"   run_experiment.py exited {rc}: {tail.strip().splitlines()[-1] if tail.strip() else ''}")
                    if i % 20 == 0 or i == len(futs):
                        el = time.time() - t0
                        print(f"   {i}/{len(futs)} runs of this level, {done_runs / el * 3600:.0f} runs/h overall")
            except KeyboardInterrupt:
                print("\ninterrupted: cancelling pending runs (relaunch to resume)")
                for fu in futs:
                    fu.cancel()
                raise
        if failures > len(order) // 10 + 3:
            sys.exit(f"{failures} runs failed at this level: check the environment before continuing")

        nxt = []
        for p in level:
            k, r = plan(p)
            c = counts(db, f"disc:{p}:K{k}")
            n = sum(c.values())
            bad = n - c.get("masked_error", 0)
            hit = (bad if a.split_on == "any" else c.get("sdc", 0)) > 0
            node = tree["nodes"][p]
            if not hit:
                status = "closed"
            elif node.get("pseudo"):
                status = "vulnerable-stratum"
            elif node["leaf"] or not node["children"]:
                status = "vulnerable-signal"
            elif depth == a.max_depth:
                status = "vulnerable-maxdepth"
            elif node["bits"] <= a.min_split_bits:
                status = "vulnerable-stratum"
            else:
                kids = expand(tree, p, a.min_split_bits)
                if kids:
                    status = "expanded"
                    nxt.extend(kids)
                else:
                    status = "vulnerable-stratum"
            pr = bad / n if n else 0.0
            apf = 1 - (1 - pr) ** (1 / k) if pr < 1 else 1.0
            visited.append((depth, p, node["bits"], k, n, c.get("masked_error", 0), c.get("sdc", 0),
                            c.get("crash", 0), c.get("timeout", 0), pr, apf, status))
        print(f"   level {depth}: {sum(1 for v in visited if v[0] == depth and v[-1] == 'expanded')} expanded, "
              f"{sum(1 for v in visited if v[0] == depth and v[-1] == 'closed')} closed, "
              f"{sum(1 for v in visited if v[0] == depth and v[-1].startswith('vulnerable'))} vulnerable leaves")
        level, depth = nxt, depth + 1

        with open(report_path, "w") as f:
            f.write("depth\tnode\tbits\tshare\tK\truns\tmasked\tsdc\tcrash\ttimeout\tp_run\ta_per_fault\tstatus\n")
            for d, p, bits, k, n, m, s, cr, to, pr, apf, st in visited:
                f.write(f"{d}\t{pretty(p)}\t{bits}\t{bits / total:.6f}\t{k}\t{n}\t{m}\t{s}\t{cr}\t{to}\t{pr:.4f}\t{apf:.5f}\t{st}\n")
    print(f"\nreport: {report_path}  ({len(visited)} nodes, {done_runs} runs in {(time.time() - t0) / 3600:.2f} h)")


if __name__ == "__main__":
    main()