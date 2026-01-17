#!/bin/bash
# Source this file to set up Android SDK environment for this project
# Usage: source set-android-env.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_DIR="${SCRIPT_DIR}/android-sdk"

if [ ! -d "${SDK_DIR}" ]; then
    echo "Error: Android SDK not found at ${SDK_DIR}"
    echo "Run ./setup-android-sdk.sh first"
    return 1 2>/dev/null || exit 1
fi

export ANDROID_SDK_ROOT="${SDK_DIR}"
export ANDROID_HOME="${SDK_DIR}"
export PATH="${SDK_DIR}/cmdline-tools/latest/bin:${SDK_DIR}/platform-tools:${PATH}"

# ADB keys stored in project directory for portability
export ADB_VENDOR_KEYS="${SCRIPT_DIR}/android-sdk/keys"
mkdir -p "${ADB_VENDOR_KEYS}"

echo "Android SDK environment configured:"
echo "  ANDROID_SDK_ROOT=${ANDROID_SDK_ROOT}"
echo "  ANDROID_HOME=${ANDROID_HOME}"
echo "  ADB_VENDOR_KEYS=${ADB_VENDOR_KEYS}"
