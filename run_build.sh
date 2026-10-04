#!/bin/bash
#
# Build Vortex rtlsim with fault injection.
#
#   ./build.sh          fresh build (stage 1 + stage 2), or stage 2 only if build/ exists
#   ./build.sh clean    remove build/ and reset the vortex submodule
#
# Stage 2 is now idempotent: processor.cpp and the rtlsim Makefile are reset
# from git and re-patched on every run, and a patch that does not apply stops
# the build (it used to be silently ignored with `|| true`).
#
# The fault ruler (fault_targets.cpp) is generated from the Verilator header
# of the previous build. If rebuilding the runtime produces a different header
# (RTL or configuration changed), the ruler is regenerated and the runtime is
# rebuilt a second time, so ruler and binary always match.
#
# A copy of the header the ruler was generated from is kept in
#   build/ruler_header/Vrtlsim_shim___024root.h        (current)
#   build/ruler_header/Vrtlsim_shim___024root.prev.h   (previous build)
# find_bit.py / resolve_injection_symbols.py need the header matching the DB.

# Vortex hardware configuration
NUM_CORES=${NUM_CORES:-2}
NUM_WARPS=${NUM_WARPS:-2}
NUM_THREADS=${NUM_THREADS:-8}

CONFIGS="-DNUM_CORES=${NUM_CORES} \
-DNUM_WARPS=${NUM_WARPS} \
-DNUM_THREADS=${NUM_THREADS}"

set -eo pipefail

XLEN=32
TOOLDIR="$HOME/tools"

ROOT_DIR=$(pwd)
BUILD_DIR="$ROOT_DIR/build"

STAGE1_PATCH="$ROOT_DIR/patches/sim_rtlsim_makefile_stage1.patch"
STAGE2_MAKEFILE_PATCH="$ROOT_DIR/patches/sim_rtlsim_makefile_stage2.patch"
PROCESSOR_PATCH="$ROOT_DIR/patches/sim_rtlsim_processor_cpp.patch"
# vxbin.py: unique temp file instead of /tmp/temp_kernel.bin (races between parallel runs)
VXBIN_PATCH="$ROOT_DIR/patches/kernel_vxbin.patch"
VXBIN_FILE="$ROOT_DIR/vortex/kernel/scripts/vxbin.py"

VORTEX_DIR="$ROOT_DIR/vortex"
TARGET_FILE="$VORTEX_DIR/sim/rtlsim/Makefile"
RTLSIM_DIR="$VORTEX_DIR/sim/rtlsim"

VL_HEADER="$BUILD_DIR/runtime/librtlsim.so.obj_dir/Vrtlsim_shim___024root.h"
RULER_HEADER_DIR="$BUILD_DIR/ruler_header"


# apply_patch <target file> <patch file>: fails the build if it does not apply
apply_patch() {
    local target="$1" patchfile="$2"
    if [ ! -f "$patchfile" ]; then
        echo "  (no patch $patchfile, skipping)"
        return 0
    fi
    echo "  applying $(basename "$patchfile") -> ${target#$ROOT_DIR/}"
    # --fuzz=0: on a pristine file a correct patch applies exactly; fuzz could
    # silently put a hunk in the wrong place
    if ! patch --forward --batch --fuzz=0 "$target" "$patchfile"; then
        echo "ERROR: $(basename "$patchfile") does not apply to ${target#$ROOT_DIR/}." >&2
        echo "       Regenerate it against the pristine file, e.g.:" >&2
        echo "       git -C vortex diff -- ${target#$VORTEX_DIR/} > $patchfile" >&2
        exit 1
    fi
    # patch leaves .orig/.rej files around on fuzz; keep the tree clean
    rm -f "$target.orig" "$target.rej"
}

# reset_from_git <path relative to vortex/>
reset_from_git() {
    git -C "$VORTEX_DIR" checkout -- "$1"
}

generate_fault_targets() {
    echo "=== Generating fault targets from ${VL_HEADER#$ROOT_DIR/} ==="
    if [ ! -f "$VL_HEADER" ]; then
        echo "ERROR: Verilator header not found: $VL_HEADER" >&2
        exit 1
    fi
    python3 \
        "$ROOT_DIR/scripts/extract_variables.py" \
        "$VL_HEADER" \
        "$RTLSIM_DIR/fault_targets.cpp" \
        --debug-file "$BUILD_DIR/valid_signals_extracted.txt"

    # keep the header the ruler was generated from (needed by the bit mappers)
    mkdir -p "$RULER_HEADER_DIR"
    if [ -f "$RULER_HEADER_DIR/Vrtlsim_shim___024root.h" ] && \
       ! cmp -s "$VL_HEADER" "$RULER_HEADER_DIR/Vrtlsim_shim___024root.h"; then
        cp "$RULER_HEADER_DIR/Vrtlsim_shim___024root.h" "$RULER_HEADER_DIR/Vrtlsim_shim___024root.prev.h"
    fi
    cp "$VL_HEADER" "$RULER_HEADER_DIR/Vrtlsim_shim___024root.h"
    cp "$VL_HEADER" "$RULER_HEADER_DIR/used_for_ruler.h"
}

rebuild_runtime() {
    echo "=== Rebuilding runtime ==="
    cd "$BUILD_DIR"
    ../vortex/configure --xlen=$XLEN --tooldir=$TOOLDIR

    CONFIGS="$CONFIGS" \
    make -C runtime clean

    CONFIGS="$CONFIGS" \
    make -C runtime -j$(nproc)
    cd "$ROOT_DIR"
}


run_stage2() {
    echo "=== Sourcing Environment Variables ==="
    if [ -f "$BUILD_DIR/ci/toolchain_env.sh" ]; then
        source "$BUILD_DIR/ci/toolchain_env.sh"
    fi

    generate_fault_targets

    echo "=== Applying Stage 2 patches (from pristine files) ==="
    reset_from_git sim/rtlsim/processor.cpp
    apply_patch "$RTLSIM_DIR/processor.cpp" "$PROCESSOR_PATCH"

    reset_from_git kernel/scripts/vxbin.py
    apply_patch "$VXBIN_FILE" "$VXBIN_PATCH"

    # the Makefile carries stage 1 + stage 2 changes: rebuild both from git
    reset_from_git sim/rtlsim/Makefile
    apply_patch "$TARGET_FILE" "$STAGE1_PATCH"
    apply_patch "$TARGET_FILE" "$STAGE2_MAKEFILE_PATCH"

    echo "=== Copying instrumentation sources into rtlsim root ==="
    cp -a "$ROOT_DIR/src/." "$RTLSIM_DIR/"
    cp -a "$ROOT_DIR/include/." "$RTLSIM_DIR/"

    echo "=== Replacing vecadd regression test ==="
    VECADD_SRC="$ROOT_DIR/tests/vecadd.cpp"
    VECADD_DIR="$ROOT_DIR/vortex/tests/regression/vecadd"
    VECADD_MAIN="$VECADD_DIR/main.cpp"

    if [ -f "$VECADD_SRC" ] && [ -d "$VECADD_DIR" ]; then
        # back up the ORIGINAL only once (it used to be overwritten on every
        # stage-2 run by the already replaced file)
        if [ -f "$VECADD_MAIN" ] && [ ! -f "$VECADD_MAIN.old" ]; then
            cp "$VECADD_MAIN" "$VECADD_MAIN.old"
            echo "Backed up $VECADD_MAIN -> $VECADD_MAIN.old"
        fi
        cp "$VECADD_SRC" "$VECADD_MAIN"
        echo "Copied $VECADD_SRC -> $VECADD_MAIN"
    else
        echo "Warning: vecadd source or destination directory missing, skipping."
    fi

    echo "=== Rebuilding tests ==="
    cd "$BUILD_DIR"

    CONFIGS="$CONFIGS" \
    make -C tests clean

    CONFIGS="$CONFIGS" \
    make -C tests -j$(nproc)
    cd "$ROOT_DIR"

    rebuild_runtime

    # The runtime rebuild re-runs Verilator: if the header changed, the ruler
    # compiled into the binary was generated from a stale header. Regenerate
    # and rebuild once more.
    if ! cmp -s "$VL_HEADER" "$RULER_HEADER_DIR/used_for_ruler.h"; then
        echo "=== Verilator header changed during the rebuild: regenerating the ruler ==="
        generate_fault_targets
        rebuild_runtime
        if ! cmp -s "$VL_HEADER" "$RULER_HEADER_DIR/used_for_ruler.h"; then
            echo "ERROR: Verilator header still differs after the second pass." >&2
            exit 1
        fi
    fi

    echo "=== DONE! ==="
    echo "Modified rtlsim runtime is ready in:"
    echo "  $BUILD_DIR/runtime"
    echo "Header matching this ruler (for find_bit.py / resolve_injection_symbols.py):"
    echo "  $RULER_HEADER_DIR/Vrtlsim_shim___024root.h"
}


# Handle: Clean
if [ "$1" = "clean" ]; then
    echo "=== Cleaning build ==="
    if [ -d "$BUILD_DIR" ]; then
        cd "$BUILD_DIR" && make clean || true
        rm -rf "$BUILD_DIR"
    fi

    echo "Resetting Vortex submodule..."
    cd "$ROOT_DIR/vortex"
    git checkout .
    git clean -fd

    echo "=== CLEAN DONE ==="
    exit 0
fi

# Handle: Existing Build (Perform Stage 2 only)
if [ -d "$BUILD_DIR" ]; then
    echo "=== Existing build found: Running Stage 2 only ==="
    run_stage2
    exit 0
fi

# Handle: Fresh Build (Stage 1 + Stage 2)
# Stage 1 builds the pristine rtlsim (it only produces the Verilator header the
# ruler is generated from): processor.cpp must be the ORIGINAL one, because the
# fault-injection sources are copied into rtlsim only in stage 2.
echo "=== Resetting rtlsim sources to pristine ==="
reset_from_git sim/rtlsim/processor.cpp
rm -f "$RTLSIM_DIR/fault_targets.cpp"
# remove instrumentation files copied by a previous stage 2 (only untracked
# ones: a file that belongs to vortex is restored from git instead)
for f in "$ROOT_DIR"/src/* "$ROOT_DIR"/include/*; do
    [ -e "$f" ] || continue
    rel="sim/rtlsim/$(basename "$f")"
    if git -C "$VORTEX_DIR" ls-files --error-unmatch "$rel" >/dev/null 2>&1; then
        reset_from_git "$rel"
    else
        rm -f "$VORTEX_DIR/$rel"
    fi
done

echo "=== Applying Stage 1 patches ==="
reset_from_git kernel/scripts/vxbin.py
apply_patch "$VXBIN_FILE" "$VXBIN_PATCH"
reset_from_git sim/rtlsim/Makefile
apply_patch "$TARGET_FILE" "$STAGE1_PATCH"

echo "=== Creating build dir and running configure ==="
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
../vortex/configure --xlen=$XLEN --tooldir=$TOOLDIR

echo "=== Checking Toolchain ==="
TOOLCHAIN_GCC="$TOOLDIR/riscv${XLEN}-gnu-toolchain/bin/riscv${XLEN}-unknown-elf-gcc"
if [ -f "$TOOLCHAIN_GCC" ]; then
    echo "Toolchain already installed."
else
    ./ci/toolchain_install.sh --all
fi

echo "=== Sourcing Environment Variables ==="
source ./ci/toolchain_env.sh


echo "=== Hardware configuration ==="
echo "$CONFIGS"

echo "=== Stage 1 compilation ==="
CONFIGS="$CONFIGS" \
make -s -j$(nproc)
echo "=== Stage 1 completed successfully ==="

cd "$ROOT_DIR"

# Hand off to Stage 2
run_stage2
