#!/bin/bash
# Build script that sets up local Android SDK and JDK environment and builds the app
# All tools are installed locally in this project directory - nothing global

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_DIR="${SCRIPT_DIR}/android-sdk"
JDK_DIR="${SCRIPT_DIR}/jdk-local/jdk-17.0.2"

# Check if SDK is installed
if [ ! -d "${SDK_DIR}/platform-tools" ]; then
    echo "Android SDK not found. Running setup..."
    "${SCRIPT_DIR}/setup-android-sdk.sh"
fi

# Check if JDK 17 is installed
if [ ! -d "${JDK_DIR}" ]; then
    echo "JDK 17 not found. Downloading..."
    mkdir -p "${SCRIPT_DIR}/jdk-local"
    curl -L "https://download.java.net/java/GA/jdk17.0.2/dfd4a8d0985749f896bed50d7138ee7f/8/GPL/openjdk-17.0.2_linux-x64_bin.tar.gz" | \
        tar -xz -C "${SCRIPT_DIR}/jdk-local"
    echo "JDK 17 installed."
fi

# Set environment (all local)
export JAVA_HOME="${JDK_DIR}"
export ANDROID_SDK_ROOT="${SDK_DIR}"
export ANDROID_HOME="${SDK_DIR}"
export PATH="${JAVA_HOME}/bin:${SDK_DIR}/cmdline-tools/latest/bin:${SDK_DIR}/platform-tools:${PATH}"

# Create local.properties for Gradle
echo "sdk.dir=${SDK_DIR}" > "${SCRIPT_DIR}/local.properties"

echo "=== Building Android App ==="
echo "Using JDK at: ${JAVA_HOME}"
echo "Using SDK at: ${SDK_DIR}"
echo ""

cd "${SCRIPT_DIR}"
chmod +x ./gradlew

echo "Running Gradle build..."
./gradlew assembleDebug --no-daemon

echo ""
echo "=== Build Complete ==="
echo "APK location: ${SCRIPT_DIR}/app/build/outputs/apk/debug/app-debug.apk"
echo "APK size: $(du -h app/build/outputs/apk/debug/app-debug.apk | cut -f1)"
