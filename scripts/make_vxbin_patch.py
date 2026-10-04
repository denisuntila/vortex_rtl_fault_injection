#!/usr/bin/env python3
"""
make_vxbin_patch.py — make vortex/kernel/scripts/vxbin.py safe for parallel runs.

vxbin.py (called by pocl at every clBuildProgram) writes the objcopy output to
a FIXED path (/tmp/temp_kernel.bin) and deletes it afterwards: two simulations
compiling at the same moment remove each other's file -> FileNotFoundError ->
clBuildProgram fails -> "crash" before any injection.

This script rewrites the assignment of `temp_bin_path` to use a unique file
from tempfile.mkstemp and adds `import tempfile`. It refuses to touch the file
if the assignment is not found exactly once, and it is idempotent.

Usage (from the repo root):
  python3 scripts/make_vxbin_patch.py vortex/kernel/scripts/vxbin.py
  git -C vortex diff -- kernel/scripts/vxbin.py > patches/kernel_vxbin.patch
"""

import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "vortex/kernel/scripts/vxbin.py"
src = open(path, encoding="utf-8").read()

if "tempfile.mkstemp" in src:
    print(f"{path}: already patched, nothing to do")
    sys.exit(0)

pat = re.compile(r"^([ \t]*)temp_bin_path[ \t]*=[ \t]*[^\n]*$", re.M)
matches = pat.findall(src)
if len(matches) != 1:
    sys.exit(f"ERROR: expected exactly one 'temp_bin_path = ...' line in {path}, found {len(matches)}. "
             f"Edit it by hand.")

src = pat.sub(lambda m: (f'{m.group(1)}# unique temp file: a fixed path races between parallel runs\n'
                         f'{m.group(1)}_fd, temp_bin_path = tempfile.mkstemp(prefix="vxbin_", suffix=".bin")\n'
                         f'{m.group(1)}os.close(_fd)'), src)

if not re.search(r"^import tempfile\b", src, re.M):
    lines = src.split("\n")
    # insert after the last top-level import in the header
    idx = 0
    for i, line in enumerate(lines[:60]):
        if re.match(r"(import|from)\s+\w", line):
            idx = i + 1
    if idx == 0:  # no import found: after shebang/docstring start
        idx = 1 if lines and lines[0].startswith("#!") else 0
    lines.insert(idx, "import tempfile")
    src = "\n".join(lines)

if not re.search(r"^import os\b|^import .*\bos\b", src, re.M):
    sys.exit("ERROR: vxbin.py does not import os (needed for os.close); edit it by hand.")

open(path, "w", encoding="utf-8").write(src)
print(f"{path}: patched (temp_bin_path now from tempfile.mkstemp)")

