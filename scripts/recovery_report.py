#!/usr/bin/env python3
"""
recovery_report.py - read-only analysis of a v1r (sentinel + relaunch) campaign.

Classifies every run:
  recovered    SDC before recovery, sentinel fired, output correct after relaunch
  incomplete   SDC before, sentinel fired, output still wrong after relaunch
  undetected   SDC before, sentinel silent
  false_alarm  correct before, sentinel fired (one relaunch, output still correct)
  masked       correct, sentinel silent
  <other>      crash / timeout (no sentinel record)
and maps the injected bit of each run onto its signal through ruler_tree.json.

Usage (repo root):
  python3 scripts/recovery_report.py --db tests/ocl_test/recovery.db --tree ruler_tree.json \
      [--campaign strat:v1r:sched_ctrl:K1] [--golden-cycles 70198]
"""
import argparse, bisect, json, math, sqlite3
from collections import Counter, defaultdict

CATS = ["recovered", "incomplete", "undetected", "false_alarm", "masked"]


def cp(k, n, conf=0.95):
    """two-sided Clopper-Pearson interval via bisection on the binomial tail"""
    if n == 0:
        return 0.0, 1.0
    a = (1 - conf) / 2

    lg = [math.lgamma(i + 1) for i in range(n + 1)]

    def cdf(j, p):  # P(X <= j), in log space (n can be large)
        if p <= 0:
            return 1.0
        if p >= 1:
            return 0.0 if j < n else 1.0
        lp, lq = math.log(p), math.log1p(-p)
        return min(1.0, sum(math.exp(lg[n] - lg[i] - lg[n - i] + i * lp + (n - i) * lq) for i in range(j + 1)))

    def solve(f):  # f increasing in p, root by bisection
        lo, hi = 0.0, 1.0
        for _ in range(60):
            m = (lo + hi) / 2
            lo, hi = (m, hi) if f(m) < 0 else (lo, m)
        return (lo + hi) / 2
    low = 0.0 if k == 0 else solve(lambda p: (1 - cdf(k - 1, p)) - a)   # P(X>=k) = a
    high = 1.0 if k == n else solve(lambda p: a - cdf(k, p))            # P(X<=k) = a
    return low, high


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--tree", required=True)
    ap.add_argument("--campaign", help="test_name (default: every strat:v1r:* campaign)")
    ap.add_argument("--golden-cycles", type=int, default=70198)
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()

    t = json.load(open(a.tree))
    leaves = sorted((lo, hi, p) for p, n in t["nodes"].items() if n["leaf"] for lo, hi in n["ranges"])
    starts = [x[0] for x in leaves]

    def sig(bit):
        i = bisect.bisect_right(starts, bit) - 1
        return leaves[i][2] if i >= 0 and bit <= leaves[i][1] else "?"

    conn = sqlite3.connect(a.db, timeout=60)
    where = "c.test_name = ?" if a.campaign else "c.test_name like 'strat:v1r%'"
    # columns added for the tiered policy (ocl_sentinel -p 1); absent in older DBs
    have = {r[1] for r in conn.execute("PRAGMA table_info(Sentinel)")}
    extra = ", ".join(f"s.{c}" if c in have else f"NULL {c}" for c in ("rec_policy", "poison_before", "full_relaunch"))
    rows = conn.execute(f"""
        select c.test_name, r.run_id, r.outcome, r.cycles, s.alarm, s.status_before, s.lanes_before,
               s.rec_groups, s.rec_elements, s.rec_unresolved, {extra},
               (select group_concat(i.bit_index || '@' || i.cycle) from Injection i where i.run_id = r.run_id)
        from Run r join Campaign c on r.cid = c.id left join Sentinel s on s.run_id = r.run_id
        where {where} order by c.test_name, r.run_id""", (a.campaign,) if a.campaign else ()).fetchall()

    by_camp = defaultdict(list)
    for row in rows:
        by_camp[row[0]].append(row)

    for camp, rs in by_camp.items():
        cat_runs = defaultdict(list)
        by_poison = full = 0
        for (_, rid, outc, cyc, alarm, before, lanes, groups, elems, unres, pol, poison, fullr, inj) in rs:
            # detected = a sentinel fired, or (tiered policy) real elements were left unwritten
            poison_det = pol == 1 and (poison or 0) > 0
            det = bool(alarm) or poison_det
            if alarm is None:
                c = outc
            elif before == "sdc":
                c = ("recovered" if outc == "masked_error" else "incomplete") if det else "undetected"
                by_poison += poison_det and not alarm
            else:
                c = "false_alarm" if det else "masked"
            full += bool(fullr)
            injs = [tuple(map(int, x.split("@"))) for x in (inj or "").split(",") if x]
            cat_runs[c].append(dict(rid=rid, cyc=cyc, lanes=lanes, groups=groups, elems=elems,
                                    sigs=[sig(b) for b, _ in injs], when=[w for _, w in injs]))
        n = len(rs)
        print(f"\n=== {camp}: {n} runs")
        for c in CATS + sorted(set(cat_runs) - set(CATS)):
            if c in cat_runs:
                print(f"  {c:12s} {len(cat_runs[c]):6d}  {100 * len(cat_runs[c]) / n:5.1f}%")
        sdc = sum(len(cat_runs[c]) for c in ("recovered", "incomplete", "undetected"))
        if sdc:
            det = len(cat_runs["recovered"]) + len(cat_runs["incomplete"])
            for label, k, m in (("detected / SDC", det, sdc),
                                ("recovered / SDC", len(cat_runs["recovered"]), sdc),
                                ("recovered / detected", len(cat_runs["recovered"]), det)):
                lo, hi = cp(k, m)
                print(f"  {label:22s} {k}/{m} = {100 * k / m:5.1f}%  [95% CP {100 * lo:.1f}, {100 * hi:.1f}]")
            if by_poison:
                print(f"  SDC detected only by the POISON check (no sentinel alarm): {by_poison}")
            res = len(cat_runs["incomplete"]) + len(cat_runs["undetected"])
            print(f"  residual SDC: {sdc} -> {res}  (reduction {100 * (1 - res / sdc):.1f}%)")

        # lanes involved before recovery (lanes_before = distinct lanes with wrong real elements)
        print("  lanes_before (wrong lanes) by category:")
        for c in ("recovered", "incomplete", "undetected"):
            h = Counter(min(r["lanes"] or 0, 7) for r in cat_runs[c])
            if h:
                print(f"    {c:12s} " + "  ".join(f"{k}{'+' if k == 7 else ''}:{v}" for k, v in sorted(h.items())))

        # cost: Run.cycles is the LAST launch, i.e. the relaunch when there was one
        rel = [r for c in ("recovered", "incomplete", "false_alarm") for r in cat_runs[c]]
        if rel:
            cyc = sorted(r["cyc"] for r in rel if r["cyc"])
            grp = Counter(r["groups"] for r in rel)
            if cyc:
                print(f"  relaunch: {len(rel)} runs, cycles median {cyc[len(cyc) // 2]} "
                      f"(min {cyc[0]}, max {cyc[-1]}) = {100 * cyc[len(cyc) // 2] / a.golden_cycles:.0f}% of one launch;"
                      f" groups relaunched {dict(sorted(grp.items()))}")
            if full:
                print(f"  whole-kernel relaunches (missing sentinel slot): {full}")
            alarm_rate = len(rel) / n
            mean_rel = sum(cyc) / len(cyc) if cyc else 0
            print(f"  expected extra cost per run in this stratum: {100 * alarm_rate * mean_rel / a.golden_cycles:.2f}%"
                  f" of a launch (alarm rate {100 * alarm_rate:.1f}%)")

        for c in ("incomplete", "undetected", "false_alarm"):
            if not cat_runs[c]:
                continue
            cnt = Counter(s for r in cat_runs[c] for s in r["sigs"])
            print(f"  {c} - top signals:")
            for s_, k in cnt.most_common(a.top):
                print(f"    {k:4d}  {s_}")
        if cat_runs["incomplete"]:
            print("  incomplete - run_id, lanes_before, groups relaunched, injection cycle(s):")
            for r in cat_runs["incomplete"][:a.top]:
                print(f"    {r['rid']:6d}  lanes {r['lanes']}  groups {r['groups']}  at {r['when']}  {r['sigs'][0].split('/')[-1]}")


if __name__ == "__main__":
    main()


    