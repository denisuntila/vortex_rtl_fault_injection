#!/usr/bin/env python3
"""
Standalone test: run the fault-injection binary repeatedly until it crashes,
then verify we can correctly extract signal / expression / details from the
JSON it prints on crash.

This does NOT touch the real campaign runner or the database — it's purely
to confirm the parsing logic works before wiring the fix into run_campaign.py.

Usage:
    python3 test_crash_parsing.py --command "./vecadd_test -n 1024" \
        --timeout 30 --max-attempts 500
"""

import argparse
import json
import shlex
import signal
import subprocess
import sys


def parse_output(output: str) -> dict:
    """Fixed parser: extracts status, and for crashes, pulls signal/expression
    out of the nested 'error' object that output_crash_json() actually emits.
    """
    result = {
        "status": None,
        "instructions": None,
        "cycles": None,
        "injections": [],
        "divergent_data": [],
        "message": None,
        "signal": None,
        "expression": None,
        "error_code": None,
        "os_returncode": None,
    }

    if not output:
        return result

    # Scan for all top-level JSON objects present anywhere in stdout
    decoder = json.JSONDecoder()
    pos = 0
    found_json = []
    while pos < len(output):
        idx = output.find("{", pos)
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

    if result["status"] is None:
        result["status"] = "crash"
        result["message"] = output
        return result

    # --- THE FIX: pull diagnostic fields out of the nested "error" object ---
    if result["status"] == "crash":
        err = result.get("error")
        if isinstance(err, dict):
            result["signal"] = err.get("details")
            result["expression"] = err.get("expression")
            result["error_code"] = err.get("code")
            result["message"] = (
                f'{err.get("expression", "")}: {err.get("details", "")}'.strip(": ")
            )

    return result


def split_env_and_argv(command: str):
    """Your command is like 'LD_LIBRARY_PATH=... VORTEX_DRIVER=rtlsim ./vecadd -n64'.
    That leading VAR=value syntax is a shell feature — without shell=True we have
    to peel it off ourselves and pass it via subprocess's env= instead.

    We deliberately do NOT touch the executable path here. When --cwd is given,
    a relative path (e.g. "./vecadd") is meant to resolve against --cwd, exactly
    like manually `cd`-ing there and running it — that's the natural, expected
    behavior and subprocess already does it correctly on its own.
    """
    import os
    tokens = shlex.split(command)
    env = os.environ.copy()
    i = 0
    while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-"):
        key, _, value = tokens[i].partition("=")
        env[key] = value
        i += 1
    return tokens[i:], env


def run_once(command: str, timeout: int, cwd: str = None) -> tuple[str, dict]:
    import os
    argv, env = split_env_and_argv(command)
    returncode = None
    try:
        proc = subprocess.run(
            argv,                     # no shell=True -> returncode is trustworthy
            env=env,
            cwd=cwd,                  # e.g. the vecadd binary's own directory,
                                       # so its relative "kernel.vxbin" lookup works
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        raw = proc.stdout or ""
        returncode = proc.returncode
    except FileNotFoundError:
        resolved = os.path.join(cwd, argv[0]) if cwd and not os.path.isabs(argv[0]) else argv[0]
        print(f"\nERROR: couldn't find/execute '{argv[0]}'.", file=sys.stderr)
        print(f"  --cwd was: {cwd!r}", file=sys.stderr)
        print(f"  Resolved (approx) to: {resolved!r} relative to your launch directory.", file=sys.stderr)
        print(f"  Check the path is correct and the binary is executable (chmod +x).", file=sys.stderr)
        sys.exit(4)
    except subprocess.TimeoutExpired as e:
        raw = e.stdout or ""
        if isinstance(raw, bytes):
            raw = raw.decode(errors="replace")

    result = parse_output(raw)

    # A run that hit --timeout is a HANG, not a crash: the process was force-killed
    # (no signal it raised itself, no returncode at all in this branch) and produced
    # no status JSON because it never got the chance to finish. Faults that induce
    # infinite loops are a real, expected outcome class -- don't mislabel them as
    # "crash", or you'll conflate two very different failure modes in your stats.
    if returncode is None and result["status"] == "crash" and result["message"] == raw:
        result["status"] = "timeout"
        result["message"] = (result["message"] or "") + f"\n\n[test harness: killed after {timeout}s timeout, likely an infinite loop/hang from the injected fault]"

    # OS-level ground truth: if the process was killed by a signal, Python
    # reports returncode as the negative signal number (only reliable
    # without shell=True). Use this whenever the self-reported JSON signal
    # is missing -- exactly the case we hit with the Verilator abort.
    if returncode is not None and returncode < 0:
        os_signal = signal.Signals(-returncode).name
        result["os_returncode"] = returncode
        if result["signal"] is None:
            result["signal"] = os_signal
            if result["message"] is None:
                result["message"] = raw

    return raw, result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--command", required=True, help="Command that runs a single fault-injected trial")
    ap.add_argument("--timeout", type=int, default=30, help="Per-run timeout in seconds")
    ap.add_argument("--max-attempts", type=int, default=500, help="Give up after this many non-crash runs")
    ap.add_argument("--cwd", default=None,
                     help="Working directory to run the command from (e.g. the vecadd binary's own "
                          "directory, so its relative 'kernel.vxbin' lookup succeeds)")
    args = ap.parse_args()

    print(f"Running until a crash is produced (max {args.max_attempts} attempts)...\n")

    real_crashes = 0
    for attempt in range(1, args.max_attempts + 1):
        raw, result = run_once(args.command, args.timeout, cwd=args.cwd)
        status = result["status"]

        # Flag "crashes" that happened before any fault was even injected --
        # these are harness/setup failures, not the hardware fault you're studying.
        pre_injection = status == "crash" and not result["injections"] and "injection" not in raw
        if pre_injection:
            print(f"[attempt {attempt}] status=crash  <-- but NO injection occurred first "
                  f"(setup/config error, not a real fault-injection crash)")
            print(f"  message: {result['message']!r}")
            print("  Fix your --command/--cwd before continuing; every subsequent run will likely fail the same way.\n")
            sys.exit(3)

        print(f"[attempt {attempt}] status={status}", end="")
        if status == "timeout":
            print("  <-- HANG: fault likely caused an infinite loop, process force-killed (this is a real, "
                  "expected outcome class -- make sure your campaign runner records it as its own category, "
                  "separate from actual crashes)")
            continue
        if status != "crash":
            print()  # masked_error / sdc — keep going
            continue

        print("  <-- crash found\n")
        print("=" * 60)
        print("RAW STDOUT:")
        print("=" * 60)
        print(raw)
        print("=" * 60)
        print("PARSED RESULT:")
        print("=" * 60)
        print(json.dumps(result, indent=2))
        print("=" * 60)

        # Sanity assertions — fail loudly if the fix doesn't actually work
        ok = True
        if result["signal"] is None:
            print("FAIL: 'signal' is still None — parsing did not extract it.")
            ok = False
        if result["message"] is None:
            print("FAIL: 'message' is still None.")
            ok = False
        if result["expression"] is None:
            print("WARN: 'expression' is None (may be legitimate if a signal, not an RT_CHECK, caused this).")

        if ok:
            print("\nOK: signal and message were successfully extracted.")
            print(f"  signal     = {result['signal']!r}")
            print(f"  expression = {result['expression']!r}")
            print(f"  error_code = {result['error_code']!r}")
            print(f"  message    = {result['message']!r}")
            sys.exit(0)
        else:
            sys.exit(1)

    print(f"\nNo crash produced after {args.max_attempts} attempts. "
          f"Try increasing --max-attempts, or check that fault injection is actually enabled.")
    sys.exit(2)


if __name__ == "__main__":
    main()



