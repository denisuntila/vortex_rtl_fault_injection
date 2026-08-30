#!/usr/bin/env python3
"""
GPU fault-injection campaign runner.

Changes vs. the original version (validated against real crash output via
scripts/test_crash.py before being ported here):

  1. No more `shell=True`. Env-var assignments in --command (e.g.
     "LD_LIBRARY_PATH=... VORTEX_DRIVER=rtlsim ...") are parsed out and passed
     via subprocess's env= instead. This is required for (2) to work at all --
     with shell=True, Python's reported returncode reflects the *shell's* exit
     code, not the real terminating signal of the child.

  2. When the binary crashes without ever managing to print its own JSON
     status (e.g. a Verilator RTL assertion -> abort() on the sim thread,
     where the custom SIGABRT handler itself deadlocks trying to clean up --
     see the "Resource deadlock avoided" case), we now fall back to the
     OS-reported terminating signal via subprocess's returncode. This is what
     actually lets Crash.signal get populated instead of staying NULL.

  3. output_crash_json() nests its diagnostic fields under an "error" object
     ({"status":"crash","error":{"expression":...,"code":...,"details":...}}).
     The parser now actually reads that object instead of silently ignoring it.

  4. A run that hits --timeout is a HANG (fault likely caused an infinite
     loop), not a crash. It's now recorded as outcome="timeout" instead of
     being lumped in with real crashes -- these are mechanistically different
     failure modes and mixing them would bias your crash statistics.

  5. A "crash" that happens with zero injection events logged is a setup/
     environment failure (wrong path, missing kernel.vxbin, bad
     LD_LIBRARY_PATH, etc.), not a hardware fault. The runner now aborts the
     whole campaign immediately with a clear message instead of quietly
     filling your database with hundreds of meaningless identical rows.
"""

import argparse
import datetime
import json
import os
import re
import shlex
import signal as signal_module
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Tuple


# -------------------------------------------------
# Database Operations
# -------------------------------------------------

def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection, schema_path: str) -> None:
    with open(schema_path, "r", encoding="utf-8") as f:
        conn.executescript(f.read())


# -------------------------------------------------
# Campaign Management
# -------------------------------------------------

def get_campaign(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id FROM Campaign
        WHERE test_name=? AND cores=? AND warps=? AND threads=?
        """,
        (args.test_name, args.cores, args.warps, args.threads)
    )
    row = cur.fetchone()
    if row:
        return row[0]

    now = datetime.datetime.now()
    cur.execute(
        """
        INSERT INTO Campaign (test_name, cores, warps, threads, start_ts)
        VALUES (?, ?, ?, ?, ?)
        """,
        (args.test_name, args.cores, args.warps, args.threads, now)
    )
    conn.commit()
    return cur.lastrowid


# -------------------------------------------------
# Command execution (no shell=True -- see module docstring)
# -------------------------------------------------

def split_env_and_argv(command: str) -> Tuple[List[str], Dict[str, str]]:
    """Peel off leading VAR=value assignments (a shell feature we've lost by
    dropping shell=True) and turn them into an env dict instead. A relative
    executable path is left untouched -- it resolves against --cwd exactly
    like manually `cd`-ing there and running it would.
    """
    tokens = shlex.split(command)
    env = os.environ.copy()
    i = 0
    while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-"):
        key, _, value = tokens[i].partition("=")
        env[key] = value
        i += 1
    return tokens[i:], env


def run_command(command: str, cwd: str, timeout: int) -> Tuple[str, int]:
    """Runs one trial. Returns (raw_stdout, returncode).
    returncode is None if the run timed out (force-killed, no exit status).
    A negative returncode means the process was killed by that signal
    (-6 == SIGABRT, etc.) -- only reliable because we don't use shell=True.
    """
    argv, env = split_env_and_argv(command)
    try:
        proc = subprocess.run(
            argv, env=env, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout,
        )
        return proc.stdout or "", proc.returncode
    except FileNotFoundError:
        resolved = os.path.join(cwd, argv[0]) if cwd and not os.path.isabs(argv[0]) else argv[0]
        print(f"\nERROR: couldn't find/execute '{argv[0]}' (resolved near '{resolved}').", file=sys.stderr)
        print(f"Check --command and --cwd are correct and the binary is executable.", file=sys.stderr)
        sys.exit(4)
    except subprocess.TimeoutExpired as e:
        raw = e.stdout or ""
        if isinstance(raw, bytes):
            raw = raw.decode(errors="replace")
        return raw, None


# -------------------------------------------------
# Output Parser
# -------------------------------------------------

def strip_injection_lines(text: str) -> str:
    """Injection events are already stored structurally in the Injection table
    (queryable by bit_index/cycle) -- keeping them as raw text inside a crash
    message too is pure duplication. Strip those lines so Crash.message holds
    only the actual diagnostic content (config line, error/assertion, perf
    stats, etc.), not a second copy of data that belongs elsewhere.
    """
    inj_line = re.compile(r'^\s*\{"type"\s*:\s*"injection".*\}\s*$')
    return "\n".join(line for line in text.splitlines() if not inj_line.match(line))


def parse_output(output: str, returncode) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": None,
        "instructions": None,
        "cycles": None,
        "injections": [],
        "divergent_data": [],
        "message": None,
        "signal": None,
    }

    if not output:
        output = ""

    for line in output.splitlines():
        inj_match = re.search(
            r'"type"\s*:\s*"injection".*?"timestamp"\s*:\s*(0x[0-9a-fA-F]+|\d+).*?"bit_index"\s*:\s*(0x[0-9a-fA-F]+|\d+)',
            line
        )
        if inj_match:
            cycle_str, bit_str = inj_match.group(1), inj_match.group(2)
            result["injections"].append({
                "cycle": int(cycle_str, 16) if cycle_str.startswith(("0x", "0X")) else int(cycle_str),
                "bit_index": int(bit_str, 16) if bit_str.startswith(("0x", "0X")) else int(bit_str)
            })

        perf_match = re.search(r"PERF:\s*instrs=(-?\d+),\s*cycles=(-?\d+)", line)
        if perf_match:
            result["instructions"] = int(perf_match.group(1))
            result["cycles"] = int(perf_match.group(2))

    # Scan for any top-level JSON objects present anywhere in stdout
    decoder = json.JSONDecoder()
    pos = 0
    found_json = []
    while pos < len(output):
        idx = output.find('{', pos)
        if idx == -1:
            break
        try:
            obj, end = decoder.raw_decode(output, idx)
            found_json.append(obj)
            pos = end
        except json.JSONDecodeError:
            pos = idx + 1

    for obj in reversed(found_json):
        if isinstance(obj, dict) and "status" in obj:
            result.update(obj)
            break

    # --- FIX: pull diagnostic fields out of the nested "error" object that
    # output_crash_json() actually emits, instead of silently dropping them ---
    if result["status"] == "crash":
        err = result.get("error")
        if isinstance(err, dict):
            result["signal"] = err.get("details")
            result["message"] = (
                f'{err.get("expression", "")}: {err.get("details", "")}'.strip(": ")
            )

    no_status_found = result["status"] is None
    if no_status_found:
        result["status"] = "crash"
        result["message"] = strip_injection_lines(output)

    # --- FIX: distinguish a HANG (timeout, force-killed, no exit status) from
    # a real crash. Different failure mechanism -- don't conflate them. ---
    if returncode is None and no_status_found:
        result["status"] = "timeout"
        result["message"] = (result["message"] or "") + \
            "\n\n[campaign runner: killed after timeout -- likely an infinite loop/hang from the injected fault]"

    # --- FIX: OS-level ground truth for the terminating signal. Only reliable
    # without shell=True. Fills in Crash.signal for crashes that never managed
    # to print their own JSON (e.g. abort() on the RTL sim thread where the
    # custom handler itself deadlocks during cleanup). ---
    elif returncode is not None and returncode < 0 and result["signal"] is None:
        result["signal"] = signal_module.Signals(-returncode).name
        if no_status_found:
            result["message"] = strip_injection_lines(output)

    return result


# -------------------------------------------------
# Database Inserts
# -------------------------------------------------

def insert_run(conn: sqlite3.Connection, cid: int, result: Dict[str, Any]) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO Run (cid, instructions, cycles, outcome, injection_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            cid,
            result.get("instructions"),
            result.get("cycles"),
            result["status"],
            len(result["injections"])
        )
    )
    return cur.lastrowid


def insert_injections(conn: sqlite3.Connection, run_id: int, injections: list) -> None:
    if not injections:
        return
    conn.executemany(
        "INSERT INTO Injection (run_id, bit_index, cycle) VALUES (?, ?, ?)",
        [(run_id, inj["bit_index"], inj["cycle"]) for inj in injections]
    )


def insert_sdc(conn: sqlite3.Connection, run_id: int, result: Dict[str, Any]) -> None:
    divergences = result.get("divergent_data", [])
    if not divergences:
        return
    conn.executemany(
        """
        INSERT INTO SDC (run_id, expected_v, actual_v, hamming_dist, index_value)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (run_id, d.get("expected_value"), d.get("actual_value"),
             d.get("hamming_distance"), d.get("index"))
            for d in divergences
        ]
    )


def insert_crash(conn: sqlite3.Connection, run_id: int, result: Dict[str, Any], save_log: bool) -> None:
    # FIX: signal is now actually written, not silently dropped.
    message = result.get("message")
    conn.execute(
        "INSERT INTO Crash (run_id, signal, message, message_size) VALUES (?, ?, ?, ?)",
        (
            run_id,
            result.get("signal"),
            message if save_log else None,
            len(message) if message else 0
        )
    )


# -------------------------------------------------
# Main Routine
# -------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GPU fault injection experiment runner")

    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--schema", required=True, help="SQL schema path")

    parser.add_argument("--test-name", required=True, help="Kernel/test name")
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--warps", type=int, required=True)
    parser.add_argument("--threads", type=int, required=True)

    parser.add_argument("--command", required=True,
                         help="Command to execute, e.g. "
                              "'LD_LIBRARY_PATH=/abs/path/runtime VORTEX_DRIVER=rtlsim ./vecadd -n64'")
    parser.add_argument("--cwd", default=None,
                         help="Working directory to run --command from (relative paths in --command, "
                              "including LD_LIBRARY_PATH, resolve against this)")
    parser.add_argument("--timeout", type=int, default=None, help="Maximum execution time in seconds")

    parser.add_argument("--save-masked-injections", action="store_true", help="Store injections for masked errors")
    parser.add_argument("--save-crash-log", action="store_true", help="Store complete crash logs")

    args = parser.parse_args()

    conn = connect_db(args.db)
    init_db(conn, args.schema)
    cid = get_campaign(conn, args)

    print(f"Running: {args.command}  (cwd={args.cwd})")

    raw, returncode = run_command(args.command, args.cwd, args.timeout)
    result = parse_output(raw, returncode)

    # FIX: a "crash" with zero injections is a setup/environment failure, not
    # a hardware fault -- abort loudly instead of polluting the database.
    if result["status"] == "crash" and not result["injections"] and "injection" not in raw:
        print("\nERROR: process failed before any fault was injected -- this is a setup/config "
              "problem (bad path, missing kernel file, wrong LD_LIBRARY_PATH, etc.), not a real "
              "fault-injection crash. Fix --command/--cwd before running a campaign.", file=sys.stderr)
        print(f"  raw output: {raw!r}", file=sys.stderr)
        conn.close()
        sys.exit(3)

    run_id = insert_run(conn, cid, result)

    if result["status"] != "masked_error" or args.save_masked_injections:
        insert_injections(conn, run_id, result["injections"])

    if result["status"] == "sdc":
        insert_sdc(conn, run_id, result)
    elif result["status"] in ("crash", "timeout"):
        insert_crash(conn, run_id, result, args.save_crash_log)

    conn.commit()
    conn.close()

    print(f"Stored run {run_id}: {result['status']}"
          + (f" (signal={result['signal']})" if result.get("signal") else ""))


if __name__ == "__main__":
    main()
    

