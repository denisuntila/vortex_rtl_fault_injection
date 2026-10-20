#!/usr/bin/env bash
#
# build_sentinel.sh — builds the ocl_sentinel host and puts it, with its
# kernel, next to ocl_test (build/tests/opencl/ocl_test), so every script that
# takes BINARY_CMD (trace_pair.sh, run_campaign.sh, run_*_parallel.sh, ...)
# can run it unchanged:  BINARY_CMD='./ocl_sentinel -n16 -m16 -k16'
#
# Usage (repo root, inside the container): ./scripts/build_sentinel.sh
# Env: POCL_PATH (default /root/tools/pocl), LLVM_PREFIX (default
#      /root/tools/llvm-vortex), CXX (default g++)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

POCL_PATH="${POCL_PATH:-/root/tools/pocl}"
LLVM_PREFIX="${LLVM_PREFIX:-/root/tools/llvm-vortex}"
SRC=tests/ocl_sentinel
DST=build/tests/opencl/ocl_test
[ -d "$DST" ] || { echo "$DST missing: build ocl_test first" >&2; exit 1; }

INC="-I$POCL_PATH/include"
[ -f "$POCL_PATH/include/CL/opencl.h" ] || {
  h=$(find /root/tools /workspace/vortex -path '*/CL/opencl.h' 2>/dev/null | head -1)
  [ -n "$h" ] || { echo "CL/opencl.h not found" >&2; exit 1; }
  INC="-I$(dirname "$(dirname "$h")")"
  echo "using OpenCL headers from ${INC#-I}"
}

# pocl's libOpenCL.so depends on the LLVM shared libraries of llvm-vortex:
# -rpath-link lets the linker resolve them; at run time they come from the
# LD_LIBRARY_PATH that every campaign script already sets.
${CXX:-g++} -std=c++17 -O2 -Wall $INC "$SRC/main.cpp" \
  -L"$POCL_PATH/lib" -Wl,-rpath-link,"$POCL_PATH/lib" -Wl,-rpath-link,"$LLVM_PREFIX/lib" \
  -lOpenCL -o "$DST/ocl_sentinel"
cp "$SRC/kernel_sentinel.cl" "$DST/kernel_sentinel.cl"
echo "built $DST/ocl_sentinel (+ kernel_sentinel.cl)"
