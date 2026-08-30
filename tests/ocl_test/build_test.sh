#!/usr/bin/env bash

set -e

# Extract the current directory's name
TEST_NAME=$(basename "$PWD")
TARGET_DIR="../../build/tests/opencl/$TEST_NAME"

# Check for Makefile in current directory
if [ ! -f "Makefile" ]; then
    echo "Error: Makefile not found in current directory."
    exit 1
fi

# Clean command handler
if [ "$1" = "clean" ] || [ "$1" = "-c" ] || [ "$1" = "--clean" ]; then
    if [ -d "$TARGET_DIR" ]; then
        echo "Cleaning build for '$TEST_NAME'..."
        make -C "$TARGET_DIR" clean
    else
        echo "Build directory '$TARGET_DIR' does not exist. Nothing to clean."
    fi
    exit 0
fi

# Build process
echo "Creating build directory: $TARGET_DIR"
mkdir -p "$TARGET_DIR"

echo "Copying Makefile..."
cp Makefile "$TARGET_DIR/"

echo "Building '$TEST_NAME'..."
make -C "$TARGET_DIR" all