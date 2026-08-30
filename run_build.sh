#!/bin/bash

# Vortex hardware configuration
NUM_CORES=${NUM_CORES:-2}
NUM_WARPS=${NUM_WARPS:-2}
NUM_THREADS=${NUM_THREADS:-8}

CONFIGS="-DNUM_CORES=${NUM_CORES} \
-DNUM_WARPS=${NUM_WARPS} \
-DNUM_THREADS=${NUM_THREADS}"

set -e

XLEN=32
TOOLDIR="$HOME/tools"

ROOT_DIR=$(pwd)
BUILD_DIR="$ROOT_DIR/build"

STAGE1_PATCH="$ROOT_DIR/patches/sim_rtlsim_makefile_stage1.patch"
STAGE2_MAKEFILE_PATCH="$ROOT_DIR/patches/sim_rtlsim_makefile_stage2.patch"
PROCESSOR_PATCH="$ROOT_DIR/patches/sim_rtlsim_processor_cpp.patch"

TARGET_FILE="$ROOT_DIR/vortex/sim/rtlsim/Makefile"
RTLSIM_DIR="$ROOT_DIR/vortex/sim/rtlsim"


run_stage2() {
    echo "=== Sourcing Environment Variables ==="
    if [ -f "$BUILD_DIR/ci/toolchain_env.sh" ]; then
        source "$BUILD_DIR/ci/toolchain_env.sh"
    fi

    echo "=== Generating fault targets ==="
    python3 \
        "$ROOT_DIR/scripts/extract_variables.py" \
        "$BUILD_DIR/runtime/librtlsim.so.obj_dir/Vrtlsim_shim___024root.h" \
        "$RTLSIM_DIR/fault_targets.cpp" \
        --debug-file "$BUILD_DIR/valid_signals_extracted.txt"

    echo "=== Applying Stage 2 patches ==="
    [ -f "$PROCESSOR_PATCH" ] && patch -N "$RTLSIM_DIR/processor.cpp" "$PROCESSOR_PATCH" || true
    [ -f "$STAGE2_MAKEFILE_PATCH" ] && patch -N "$TARGET_FILE" "$STAGE2_MAKEFILE_PATCH" || true

    echo "=== Copying instrumentation sources into rtlsim root ==="
    cp -a "$ROOT_DIR/src/." "$RTLSIM_DIR/"
    cp -a "$ROOT_DIR/include/." "$RTLSIM_DIR/"

    echo "=== Replacing vecadd regression test ==="
    VECADD_SRC="$ROOT_DIR/tests/vecadd.cpp"
    VECADD_DIR="$ROOT_DIR/vortex/tests/regression/vecadd"
    VECADD_MAIN="$VECADD_DIR/main.cpp"

    if [ -f "$VECADD_SRC" ] && [ -d "$VECADD_DIR" ]; then
        if [ -f "$VECADD_MAIN" ]; then
            mv "$VECADD_MAIN" "$VECADD_MAIN.old"
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

    echo "=== Rebuilding runtime ==="
    ../vortex/configure --xlen=$XLEN --tooldir=$TOOLDIR

    CONFIGS="$CONFIGS" \
    make -C runtime clean

    CONFIGS="$CONFIGS" \
    make -C runtime -j$(nproc)

    echo "=== DONE! ==="
    echo "Modified rtlsim binary is ready at:"
    echo "  $BUILD_DIR/sim/rtlsim/rtlsim"
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
echo "=== Applying Stage 1 Makefile patch ==="
[ -f "$STAGE1_PATCH" ] && patch "$TARGET_FILE" "$STAGE1_PATCH"

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

# Hand off to Stage 2
run_stage2
